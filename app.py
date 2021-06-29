from base64 import b64encode
from os import path, listdir, getenv
from flask import Flask, request
from requests import post, delete, get, Session, adapters
from requests.auth import HTTPBasicAuth as basicAuth
from jinja2 import Environment, PackageLoader
from json import loads

env = Environment(loader=PackageLoader("app"), autoescape=False)
app = Flask(__name__)
dashboard_urls = {}
dashboard_uids = {}
dashboard_ids = {}
user_ids = {}
gf_endpoint = getenv("GF_ADDRESS", "grafana") + ':' + getenv("GF_PORT", "3000")
gf_admin_user = getenv("GF_ADMIN_USER", "admin")
gf_admin_pw = getenv("GF_ADMIN_PW", "admin")
oidc_client_id = getenv("OIDC_CLIENT_ID", "sodalite-ide")
oidc_client_secret = getenv("OIDC_CLIENT_SECRET", "")
oidc_introspection_endpoint = getenv("OIDC_INTROSPECTION_ENDPOINT", "")

session = Session()
adapter = adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
for protocol in ['http:', 'https:']:
    session.mount(protocol, adapter)


@app.route('/dashboards', methods=['POST'])
def create_dashboards():
    try:
        user_info = _token_info(_get_token(request))
    except Exception as e:
        return str(e), 500
    if not user_info:
        return "Unauthorized access\n", 401

    user_email = user_info['email']
    user_name = user_info['name']
    json_data = request.json

    if 'deployment_label' in json_data and 'deployment_label' in json_data:
        deployment_label = json_data['deployment_label']
        deployment_id = json_data['deployment_id']
    else:
        return "Request must include deployment_label and deployment_id\n", 403

    if not _check_user_deployment_availability(user_email, deployment_id):
        return "Deployment label already belongs to a different user\n", 403

    if user_email not in dashboard_uids:
        dashboard_urls[user_email] = {}
        dashboard_uids[user_email] = {}
        dashboard_ids[user_email] = {}
        user_id = _get_user_id(user_email, user_name)
        if user_id is None:
            return "Could not register user in Grafana\n", 500
        user_ids[user_email] = user_id

    dashboard_urls[user_email][deployment_id] = {}
    dashboard_uids[user_email][deployment_id] = {}
    dashboard_ids[user_email][deployment_id] = {}

    for template_file in listdir(path.dirname(path.abspath(__file__)) + '/templates'):
        dashboard_type = path.splitext(path.splitext(template_file)[0])[0]
        template = env.get_template(template_file)

        # Create of the dashboard with a dummy dashboard uid and no url in the links
        dashboard = template.render(deployment_label=deployment_label,
                                    deployment_id=deployment_id,
                                    dashboard_url="/",
                                    dashboard_uid="null")
        r = post('http://' + gf_endpoint + '/api/dashboards/db',
                 auth=basicAuth(gf_admin_user, gf_admin_pw),
                 json=loads(dashboard))
        r_json = r.json()
        dashboard_data = {
            "uid": r_json['uid'],
            "url": r_json['url'],
            "id": str(r_json['id'])
        }
        dashboard_urls[user_email][deployment_id][dashboard_type] = "http://" + gf_endpoint + dashboard_data["url"]
        dashboard_uids[user_email][deployment_id][dashboard_type] = dashboard_data["uid"]
        dashboard_ids[user_email][deployment_id][dashboard_type] = dashboard_data["id"]

        # Update the dashboard to include the dashboard url in the links and real uid
        dashboard = template.render(deployment_label=deployment_label,
                                    deployment_id=deployment_id,
                                    dashboard_url=dashboard_data["url"],
                                    dashboard_uid='"' + dashboard_data["uid"] + '"')
        post('http://' + gf_endpoint + '/api/dashboards/db',
             auth=basicAuth(gf_admin_user, gf_admin_pw),
             json=loads(dashboard))

        # Set the permissions
        post('http://' + gf_endpoint + '/api/dashboards/id/' + dashboard_data["id"] + '/permissions',
             auth=basicAuth(gf_admin_user, gf_admin_pw),
             json={"items": [{"userId": user_ids[user_email], "permission": 1}]})

    return "Dashboards added\n", 200


@app.route('/dashboards', methods=['DELETE'])
def delete_dashboards():
    try:
        user_info = _token_info(_get_token(request))
    except Exception as e:
        return str(e), 500
    if not user_info:
        return "Access not authorized\n", 401

    user_email = user_info['email']

    json_data = request.json

    if 'deployment_label' in json_data and 'deployment_label' in json_data:
        deployment_id = json_data['deployment_id']
    else:
        return "Request must include deployment_label and deployment_id\n", 403

    if user_email not in dashboard_uids or deployment_id not in dashboard_uids[user_email]:
        return "Could not find the deployment_id in the user's list of dashboards\n", 404
    for dashboard in dashboard_uids[user_email][deployment_id]:
        r = delete('http://' + gf_endpoint + '/api/dashboards/uid/' +
                   dashboard_uids[user_email][deployment_id][dashboard],
                   auth=basicAuth(gf_admin_user, gf_admin_pw))
        if r.status_code != 200:
            return "Could not delete the dashboard " + dashboard + ": " + str(r.content)+"\n", r.status_code

    dashboard_urls[user_email].pop(deployment_id)
    dashboard_uids[user_email].pop(deployment_id)
    dashboard_ids[user_email].pop(deployment_id)

    return "Dashboards deleted\n", 200


@app.route('/dashboards/user', methods=['GET'])
def get_dashboards_user():
    try:
        user_info = _token_info(_get_token(request))
    except Exception as e:
        return str(e), 500
    if not user_info:
        return "Access not authorized\n", 401

    user_email = user_info['email']

    if user_email not in dashboard_urls:
        return "Could not find the user\n", 404

    return dashboard_urls[user_email], 200


@app.route('/dashboards/deployment/<deployment_id>', methods=['GET'])
def get_dashboards_deployment(deployment_id):
    try:
        user_info = _token_info(_get_token(request))
    except Exception as e:
        return str(e), 500
    if not user_info:
        return "Access not authorized\n", 401

    user_email = user_info['email']
    if not deployment_id:
        return "Must provide the deployment_id\n", 403
    if user_email not in dashboard_urls or deployment_id not in dashboard_urls[user_email]:
        return "Could not find the deployment\n", 404

    return dashboard_urls[user_email][deployment_id], 200


def _token_info(access_token) -> dict:

    req = {'token': access_token}
    headers = {'Content-type': 'application/x-www-form-urlencoded'}
    if not oidc_introspection_endpoint:
        raise Exception("No oidc_introspection_endpoint set on the server")

    basic_auth_string = '{0}:{1}'.format(oidc_client_id, oidc_client_secret)
    basic_auth_bytes = bytearray(basic_auth_string, 'utf-8')
    headers['Authorization'] = 'Basic {0}'.format(b64encode(basic_auth_bytes).decode('utf-8'))

    token_response = post(oidc_introspection_endpoint, data=req, headers=headers)
    if token_response.status_code != 200:
        raise Exception("There was a problem trying to authenticate with keycloak:\n"
                        " HTTP code: " + str(token_response.status_code) + "\n"
                        " Content:" + str(token_response.content) + "\n")
    if not token_response.ok:
        return {}
    json = token_response.json()
    if "active" in json and json["active"] is False:
        return {}
    return json


def _get_user_id(user_email, user_name):
    r = get('http://' + gf_endpoint + '/api/users/lookup?loginOrEmail=' + user_email,
            auth=basicAuth(gf_admin_user, gf_admin_pw), json={})
    if r.status_code == 200:
        return r.json()['id']
    if r.status_code == 404:
        # If the user isn't registered, register it.
        r = post('http://' + gf_endpoint + '/api/admin/users',
                 auth=basicAuth(gf_admin_user, gf_admin_pw), json={
                    "name": user_name,
                    "email": user_email,
                    "login": user_email,
                    "authLabels": ["OAuth"],
                    "password": "nothing"
                    }).json()
        if "id" in r:
            return r["id"]
    return None


def _check_user_deployment_availability(user_email, deployment_id):
    for user in dashboard_uids:
        for deployment in dashboard_uids[user]:
            if deployment_id == deployment and user != user_email:
                return False
    return True


def _get_token(r):
    auth_header = r.environ["HTTP_AUTHORIZATION"].split()
    if auth_header[0] == "Bearer":
        return auth_header[1]
    return ""
