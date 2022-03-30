import logging

import aiohttp.web
import asab.web.rest

from ..decorators import access_control

###

L = logging.getLogger(__name__)

###


class TenantHandler(object):

	def __init__(self, app, tenant_svc):
		self.App = app
		self.TenantService = tenant_svc
		self.NameProposerService = app.get_service("seacatauth.NameProposerService")

		web_app = app.WebContainer.WebApp
		web_app.router.add_get('/tenant', self.list)
		web_app.router.add_get('/tenants', self.search)
		web_app.router.add_get('/tenant/{tenant}', self.get)
		web_app.router.add_put('/tenant/{tenant}/data', self.set_data)

		web_app.router.add_post('/tenant', self.create)
		web_app.router.add_delete('/tenant/{tenant}', self.delete)

		web_app.router.add_get('/tenant_assign/{credentials_id}', self.get_tenants_by_credentials)
		web_app.router.add_put('/tenant_assign/{credentials_id}', self.set_tenants)
		web_app.router.add_post('/tenant_assign/{credentials_id}/{tenant}', self.assign_tenant)
		web_app.router.add_delete('/tenant_assign/{credentials_id}/{tenant}', self.unassign_tenant)

		web_app.router.add_get('/public/tenant_propose', self.propose_tenant)

		# Public endpoints
		web_app_public = app.PublicWebContainer.WebApp
		web_app_public.router.add_get('/tenant', self.list)
		web_app_public.router.add_get('/public/tenant_propose', self.propose_tenant)


	# IMPORTANT: This endpoint needs to be compatible with `/tenant` handler in Asab Tenant Service
	async def list(self, request):
		# TODO: This has to be cached agressivelly
		provider = self.TenantService.get_provider()
		result = []
		async for tenant in provider.iterate():
			result.append(tenant['_id'])
		return asab.web.rest.json_response(request, data=result)


	async def search(self, request):
		page = int(request.query.get("p", 1)) - 1
		limit = request.query.get("i")
		if limit is not None:
			limit = int(limit)

		provider = self.TenantService.get_provider()

		count = await provider.count()

		tenants = []
		async for tenant in provider.iterate(page, limit):
			tenants.append(tenant)

		result = {
			"result": "OK",
			"data": tenants,
			"count": count,
		}

		return asab.web.rest.json_response(request, data=result)


	async def get(self, request):
		tenant_id = request.match_info.get("tenant")
		provider = self.TenantService.get_provider()
		tenant = await provider.get(tenant_id)
		return asab.web.rest.json_response(request, data=tenant)


	@asab.web.rest.json_schema_handler({
		"type": "object",
		"properties": {
			"id": {"type": "string"},
		},
		"required": ["id"],
		"additionalProperties": False,
	})
	@access_control("authz:superuser")
	async def create(self, request, *, credentials_id, json_data):
		tenant_id = json_data["id"]

		# Create tenant
		result = await self.TenantService.create_tenant(tenant_id, creator_id=credentials_id)
		if result["result"] != "OK":
			return asab.web.rest.json_response(request, data=result, status=400)

		tenant_id = result["id"]

		# TODO: configurable name
		role_id = "{}/admin".format(tenant_id)
		role_service = self.TenantService.App.get_service("seacatauth.RoleService")

		try:
			# Create admin role in tenant
			await role_service.create(role_id)
			# Assign "authz:tenant:admin" resource
			await role_service.update_resources(role_id, resources_to_set=["authz:tenant:admin"])
		except Exception as e:
			L.error("Error creating role", struct_data={
				"role": role_id,
				"error": type(e).__name__
			})

		if credentials_id is not None:
			# Assign the tenant to the user who created it
			try:
				await self.TenantService.assign_tenant(credentials_id, tenant_id)
			except Exception as e:
				L.error("Error assigning tenant", struct_data={
					"cid": credentials_id,
					"tenant": tenant_id,
					"reason": "{}: {}".format(type(e).__name__, e)
				})
			# Assign the tenant admin role to the user
			try:
				await role_service.assign_role(credentials_id, role_id)
			except Exception as e:
				L.error("Error assigning role", struct_data={
					"cid": credentials_id,
					"role": role_id,
					"reason": "{}: {}".format(type(e).__name__, e)
				})

		return asab.web.rest.json_response(
			request,
			data=result,
			status=200
		)

	@asab.web.rest.json_schema_handler({
		"type": "object",
		"patternProperties": {
			"^.+$": {"anyOf": [
				{"type": "string"},
				{"type": "number"},
				{"type": "boolean"},
				{"type": "null"},
			]}
		},
		"additionalProperties": False,
	})
	@access_control("authz:tenant:admin")
	async def set_data(self, request, *, json_data, tenant):
		result = await self.TenantService.set_tenant_data(tenant, json_data)
		return asab.web.rest.json_response(request, data=result)


	@access_control("authz:superuser")
	async def delete(self, request, *, tenant):
		"""
		Delete a tenant. Also delete all its roles and assignments linked to this tenant.
		"""
		result = await self.TenantService.delete_tenant(tenant)
		return asab.web.rest.json_response(request, data=result)


	@asab.web.rest.json_schema_handler({
		"type": "object",
		"required": [
			"tenants",
		],
		"properties": {
			"tenants": {
				"type": "array",
				"items": {
					"type": "string",
				},
			},
		}
	})
	@access_control()
	async def set_tenants(self, request, *, json_data):
		"""
		Helper method for bulk tenant un/assignment
		"""
		credentials_id = request.match_info["credentials_id"]
		data = await self.TenantService.set_tenants(
			session=request.Session,
			credentials_id=credentials_id,
			tenants=json_data["tenants"]
		)

		return asab.web.rest.json_response(
			request,
			data=data,
			status=200 if data["result"] == "OK" else 400
		)


	@access_control("authz:tenant:admin")
	async def assign_tenant(self, request, *, tenant):
		data = await self.TenantService.assign_tenant(
			request.match_info["credentials_id"],
			tenant,
		)

		return asab.web.rest.json_response(
			request,
			data=data,
			status=200 if data["result"] == "OK" else 400
		)


	@access_control("authz:tenant:admin")
	async def unassign_tenant(self, request, *, tenant):
		data = await self.TenantService.unassign_tenant(
			request.match_info["credentials_id"],
			tenant,
		)

		return asab.web.rest.json_response(
			request,
			data=data,
			status=200 if data["result"] == "OK" else 400
		)


	async def get_tenants_by_credentials(self, request):
		result = await self.TenantService.get_tenants(request.match_info["credentials_id"])
		return asab.web.rest.json_response(
			request, result
		)


	async def propose_tenant(self, request):
		proposed_tenant = self.NameProposerService.propose_name()
		# TODO: Check is the proposed tenant name is not already taken
		return asab.web.rest.json_response(request, {'tenant_id': proposed_tenant})
