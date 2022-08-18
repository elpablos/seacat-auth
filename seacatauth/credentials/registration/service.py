import datetime
import logging
import secrets

import asab

#

L = logging.getLogger(__name__)

#


class RegistrationService(asab.Service):

	RegistrationTokenCollection = "rg"
	RegistrationTokenByteLength = 32
	RegistrationKeyByteLength = 32

	def __init__(self, app, cred_service, service_name="seacatauth.RegistrationService"):
		super().__init__(app, service_name)
		self.CredentialsService = cred_service
		self.CommunicationService = app.get_service("seacatauth.CommunicationService")
		self.AuditService = app.get_service("seacatauth.AuditService")
		self.StorageService = app.get_service("asab.StorageService")

		self.AuthWebUIBaseUrl = asab.Config.get("general", "auth_webui_base_url").rstrip("/")
		self.InviteExpiration = asab.Config.getseconds("seacatauth:registration", "expiration")

		self.RegistrationEncrypted = asab.Config.getboolean("general", "registration_encrypted")
		self.Registrations = {}

		self.App.PubSub.subscribe("Application.tick/60!", self._on_tick)


	async def _on_tick(self, event_name):
		await self.delete_expired_registration_tokens()


	async def create_registration_token(
		self, features: dict,
		provider_id: str = None,
		tenant: str = None,
		issued_by: str = None
	):
		"""
		Issue a new registration token

		:param features: Dictionary of features required for the registration
		:type features: dict
		:param provider_id: ID of the provider that the user will be registered to
		:type provider_id: str
		:param tenant: The tenant to which the user will be assigned. If not specified,
		the user will be able to create a new tenant upon registration.
		:type tenant: str
		:param issued_by: The user who issued the registration token.
		:type issued_by: str
		:return: The generated registration token.
		"""
		registration_token = secrets.token_urlsafe(self.RegistrationTokenByteLength)
		upsertor = self.StorageService.upsertor(self.RegistrationTokenCollection, registration_token)

		provider = self.get_provider(provider_id)
		assert provider is not None
		upsertor.set("p", provider.ProviderID)

		# TODO: Validate features against registration policy
		# policy = self.CredentialsService.Policy.RegistrationPolicy
		upsertor.set("f", features)

		key = secrets.token_bytes(self.RegistrationKeyByteLength)
		upsertor.set("__k", key, encrypt=True)

		if tenant is not None:
			# If no tenant is specified, the user will create a new one upon registration
			upsertor.set("t", tenant)

		if issued_by is not None:
			upsertor.set("i", issued_by)

		# TODO: Store registration IP (to limit anonymous registrations)

		expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=self.InviteExpiration)
		upsertor.set("exp", expires_at)

		await upsertor.execute(custom_data={"event_type": "registration_token_created"})

		L.log(asab.LOG_NOTICE, "Registration token created", struct_data={
			"token": registration_token,
			"provider": provider.ProviderID,
			"tenant": tenant,
			"issued_by": issued_by})

		return registration_token


	async def update_registration_token(self, token, **kwargs):
		# TODO: Not sure if this method will be needed
		raise NotImplementedError()


	async def get_registration_token(self, token):
		return await self.StorageService.get(self.RegistrationTokenCollection, token, decrypt=["__k"])


	async def delete_registration_token(self, token):
		"""
		Delete a registration token from the database

		:param token: The token generated by the registration service
		"""
		await self.StorageService.delete(self.RegistrationTokenCollection, token)
		L.log(asab.LOG_NOTICE, "Registration token deleted", struct_data={"token": token})


	async def delete_expired_registration_tokens(self):
		collection = self.StorageService.Database[self.RegistrationTokenCollection]
		query_filter = {"exp": {"$lt": datetime.datetime.now(datetime.timezone.utc)}}
		result = await collection.delete_many(query_filter)
		if result.deleted_count > 0:
			L.log(asab.LOG_NOTICE, "Expired registration tokens deleted", struct_data={
				"count": result.deleted_count})


	def get_provider(self, provider_id: str = None):
		"""
		Locate a provider that supports credentials registration

		:param provider_id: The ID of the provider to use. If not specified, the first
		provider that supports registration will be used
		:type provider_id: str
		:return: A provider object
		"""
		# Specific provider requested
		if provider_id is not None:
			provider = self.CredentialsService.Providers.get(provider_id)
			if provider.Config.getboolean("registration"):
				return provider
			else:
				L.warning("Provider does not support registration", struct_data={"provider_id": provider_id})
				return None

		# No specific provider requested; get the first one that supports registration
		for provider in self.CredentialsService.CredentialProviders.values():
			if provider.Config.getboolean("registration"):
				return provider
		else:
			L.warning("No credentials provider with enabled registration found")
			return None


	async def create_invitation(
		self,
		credentials: dict,
		tenant: str = None,
		roles: list = None,
		issued_by_cid: str = None,
		issued_by_ips: str = None,
	):
		"""
		Create an invitation into a tenant and send it to a specified email address.
		"""

		registration_token = await self.create_registration_token(
			credentials,
			tenant,
			roles,
			issued_by_cid,
			issued_by_ips,
		)

		# TODO: Send invitation via mail
		L.log(asab.LOG_NOTICE, "Sending invitation", struct_data={
			"c": credentials,
			"t": tenant,
			"r": roles,
			"issued_by_cid": issued_by_cid,
			"issued_by_ips": issued_by_ips,
		})

		return registration_token


	async def register_credentials(self, register_info: dict):
		"""
		This is an anonymous user request to register (create) new credentials
		"""
		provider = self.get_provider()
		if provider is None:
			return None
		return await provider.register(register_info)
