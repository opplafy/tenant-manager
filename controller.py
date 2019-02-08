#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Opplafy Tenant Manager
# Copyright (C) 2019  Future Internet Consulting and Development Solutions S.L.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import json
import logging

from flask import Flask, request, make_response

from lib.database import DatabaseController
from lib.keyrock_client import KeyrockClient, KeyrockError
from lib.umbrella_client import UmbrellaClient, UmbrellaError
from lib.utils import build_response, authorized
from settings import IDM_URL, IDM_PASSWD, IDM_USER, BROKER_APP_ID, \
     BAE_APP_ID, BROKER_ADMIN_ROLE, BROKER_CONSUMER_ROLE, BAE_SELLER_ROLE, \
     BAE_CUSTOMER_ROLE, BAE_ADMIN_ROLE, UMBRELLA_URL, UMBRELLA_TOKEN, UMBRELLA_KEY, \
     MONGO_URL, MONGO_PORT


app = Flask(__name__)


def _build_policy(method, tenant, role):
    return {
        "http_method": method,
        "regex": "^/",
        "settings": {
            "required_headers": [{
                "key": "Fiware-Service",
                "value": tenant
            }],
            "required_roles": [
                role
            ],
            "required_roles_override": True
        }
    }


def _create_access_policies(tenant, org_id, user_info):
    # Build read and admin policies
    read_role = org_id + '.' + BROKER_CONSUMER_ROLE
    read_policy = _build_policy('GET', tenant, read_role)

    admin_role = org_id + '.' + BROKER_ADMIN_ROLE
    admin_policy = _build_policy('any', tenant, admin_role)

    # Add new policies to existing API sub settings
    umbrella_client = UmbrellaClient(UMBRELLA_URL, UMBRELLA_TOKEN, UMBRELLA_KEY)
    umbrella_client.add_sub_url_setting_app_id(BROKER_APP_ID, [read_policy, admin_policy])


def _map_roles(member):
    roles = [BROKER_CONSUMER_ROLE]

    if member['role'] == 'owner':
        roles.append(BROKER_ADMIN_ROLE)

    return roles


@app.route("/tenant", methods=['POST'])
@authorized
def create(user_info):
    # Get tenant info for JSON request
    if 'name' not in request.json:
        return build_response({
            'error': 'Missing required field name'
        }, 422)

    if 'description' not in request.json:
        return build_response({
            'error': 'Missing required field description'
        }, 422)

    if 'users' in request.json:
        for user in request.json.get('users'):
            if 'name' not in user or 'roles' not in user:
                return build_response({
                    'error': 'Missing required field in user specification'
                }, 422)

    try:
        # Build tenant-id 
        tenant_id = request.json.get('name').lower().replace(' ', '_')
        database_controller = DatabaseController(host=MONGO_URL, port=MONGO_PORT)
        prev_t = database_controller.get_tenant(tenant_id)

        if prev_t is not None:
            return build_response({
                'error': 'The tenant {} is already registered'.format(tenant_id)
            }, 409)

        keyrock_client = KeyrockClient(IDM_URL, IDM_USER, IDM_PASSWD)
        org_id = keyrock_client.create_organization(
            request.json.get('name'), request.json.get('description'), user_info['id'])

        # Add context broker role
        keyrock_client.authorize_organization(org_id, BROKER_APP_ID, BROKER_ADMIN_ROLE, BROKER_CONSUMER_ROLE)

        # Add BAE roles
        keyrock_client.authorize_organization_role(org_id, BAE_APP_ID, BAE_SELLER_ROLE, 'owner')
        keyrock_client.authorize_organization_role(org_id, BAE_APP_ID, BAE_CUSTOMER_ROLE, 'owner')
        keyrock_client.authorize_organization_role(org_id, BAE_APP_ID, BAE_ADMIN_ROLE, 'owner')

        # Add tenant users if provided
        if 'users' in request.json:
            for user in request.json.get('users'):
                # User name is not used to identify in Keyrock
                user_id = keyrock_client.get_user_id(user['name'])

                # Keyrock IDM only supports a single organization role
                if BROKER_CONSUMER_ROLE in user['roles'] and not BROKER_ADMIN_ROLE in user['roles']:
                    keyrock_client.grant_organization_role(org_id, user_id, 'member')

                if BROKER_ADMIN_ROLE in user['roles']:
                    keyrock_client.grant_organization_role(org_id, user_id, 'owner')

        _create_access_policies(tenant_id, org_id, user_info)
        database_controller.save_tenant(
            tenant_id, request.json.get('name'), request.json.get('description'), user_info['id'], org_id)

    except (KeyrockError, UmbrellaError) as e:
        return build_response({
            'error': str(e)
        }, 400)
    except Exception:
        return build_response({
            'error': 'Unexpected error creating tenant'
        }, 500)

    return make_response('', 201)


@app.route("/tenant", methods=['GET'])
@authorized
def get(user_info):
    response_data = []
    try:
        database_controller = DatabaseController(host=MONGO_URL, port=MONGO_PORT)
        response_data = database_controller.read_tenants(user_info['id'])

        # Load tenant memebers from the IDM
        keyrock_client = KeyrockClient(IDM_URL, IDM_USER, IDM_PASSWD)
        for tenant in response_data:
            members = keyrock_client.get_organization_members(tenant['tenant_organization'])
            tenant['users'] = [{
                'id': member['user_id'],
                'name': member['name'],
                'roles': _map_roles(member)
            } for member in members]

    except:
        return build_response({
            'error': 'An error occurred reading tenants'
        }, 500)

    return build_response(response_data, 200)


@app.route("/tenant/<tenant_id>", methods=['GET'])
@authorized
def get_tenant(user_info, tenant_id):
    tenant_info = None
    try:
        database_controller = DatabaseController(host=MONGO_URL, port=MONGO_PORT)
        tenant_info = database_controller.get_tenant(tenant_id)

        if tenant_info is None:
            return build_response({
                'error': 'Tenant {} does not exist'.format(tenant_id)
            }, 404)

        if tenant_info['user_id'] != user_info['id']:
            return build_response({
                'error': 'You are not authorized to retrieve tenant info'
            }, 403)

        # Load tenant memebers from the IDM
        keyrock_client = KeyrockClient(IDM_URL, IDM_USER, IDM_PASSWD)
        members = keyrock_client.get_organization_members(tenant_info['tenant_organization'])
        tenant_info['users'] = [{
            'id': member['user_id'],
            'name': member['name'],
            'roles': _map_roles(member)
        } for member in members]

    except:
        return build_response({
            'error': 'An error occurred reading tenants'
        }, 500)

    return build_response(tenant_info, 200)


@app.route("/users", methods=['GET'])
@authorized
def get_users(user_info):
    try:
        # This method is just a proxy to the IDM for reading available users
        keyrock_client = KeyrockClient(IDM_URL, IDM_USER, IDM_PASSWD)
        return keyrock_client.get_users()
    except KeyrockError as e:
        return build_response({
            'error': str(e)
        }, 400)
    except:
        return build_response({
            'error': 'An error occurred reading tenants'
        }, 500)   


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=(os.environ.get("DEBUG", "false").strip().lower() == "true"))
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
