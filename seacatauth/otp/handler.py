import logging

import asab.web
import asab.web.rest

from ..decorators import access_control
from ..exceptions import TOTPNotActiveError

#

L = logging.getLogger(__name__)

#


class OTPHandler(object):

	def __init__(self, app, otp_svc):
		self.CredentialsService = app.get_service('seacatauth.CredentialsService')
		self.OTPService = otp_svc
		web_app_public = app.PublicWebContainer.WebApp

		web_app = app.WebContainer.WebApp
		web_app.router.add_get('/public/totp', self.prepare_totp_if_not_active)
		web_app.router.add_put('/public/set-totp', self.set_totp)
		web_app.router.add_put('/public/unset-totp', self.unset_totp)

		# Public endpoints
		web_app_public.router.add_get('/public/totp', self.prepare_totp_if_not_active)
		web_app_public.router.add_put('/public/set-totp', self.set_totp)
		web_app_public.router.add_put('/public/unset-totp', self.unset_totp)

	@access_control()
	async def prepare_totp_if_not_active(self, request, *, credentials_id):
		"""
		Return the status of TOTP setting.
		If not activated, generate and return a new TOTP secret.
		"""
		if await self.OTPService.has_activated_totp(credentials_id):
			response: dict = {
				"result": "OK",
				"active": True
			}
		else:
			response: dict = await self.OTPService.prepare_totp(request.Session, credentials_id)
			response.update({
				"result": "OK",
				"active": False
			})

		return asab.web.rest.json_response(request, response)

	@asab.web.rest.json_schema_handler({
		'type': 'object',
		'required': ['otp'],
		'properties': {
			'otp': {'type': 'string'}
		}
	})
	@access_control()
	async def set_totp(self, request, *, credentials_id, json_data):
		"""
		Activates TOTP for the current user, provided that a TOTP secret is already set.
		"""
		otp = json_data.get("otp")
		response = await self.OTPService.activate_prepared_totp(request.Session, credentials_id, otp)
		return asab.web.rest.json_response(request, response)


	@access_control()
	async def unset_totp(self, request, *, credentials_id):
		"""
		Deactivates TOTP for the current user and erases the secret.
		"""
		try:
			await self.OTPService.deactivate_totp(credentials_id)
		except TOTPNotActiveError:
			return asab.web.rest.json_response(request, {"result": "FAILED"}, status=400)

		return asab.web.rest.json_response(request, {"result": "OK"})
