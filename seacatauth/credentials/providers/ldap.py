import logging
import base64
import datetime
import contextlib
import typing

from typing import Optional


import ldap
import ldap.resiter
import ldap.filter

import asab
import asab.proactor

from .abc import CredentialsProviderABC

#

L = logging.getLogger(__name__)

#


_TLS_VERSION = {
	"1.0": ldap.OPT_X_TLS_PROTOCOL_TLS1_0,
	"1.1": ldap.OPT_X_TLS_PROTOCOL_TLS1_1,
	"1.2": ldap.OPT_X_TLS_PROTOCOL_TLS1_2,
	"1.3": ldap.OPT_X_TLS_PROTOCOL_TLS1_3,
}


class LDAPCredentialsService(asab.Service):

	def __init__(self, app, service_name="seacatauth.credentials.ldap"):
		super().__init__(app, service_name)
		app.add_module(asab.proactor.Module)


	def create_provider(self, provider_id, config_section_name):
		proactor_svc = self.App.get_service("asab.ProactorService")
		return LDAPCredentialsProvider(provider_id, config_section_name, proactor_svc)


class LDAPCredentialsProvider(CredentialsProviderABC):

	Type = "ldap"

	ConfigDefaults = {
		"uri": "ldap://localhost:389/",  # Multiple URIs need to be separated by comma or whitespace
		"network_timeout": "10",  # set network_timeout to -1 for no timeout
		"username": "cn=admin,dc=example,dc=org",
		"password": "admin",
		"base": "dc=example,dc=org",
		"filter": "(&(objectClass=inetOrgPerson)(cn=*))",  # should filter valid users only
		"attributes": "mail mobile",

		# Path to CA file in PEM format
		"tls_cafile": "",

		# Certificate policy.
		# Possible options (from python-ldap docs):
		# "never"  - Don’t check server cert and host name
		# "allow"  - Used internally by slapd server.
		# "demand" - Validate peer cert chain and host name
		# "hard"   - Same as "demand"
		"tls_require_cert": "never",

		"tls_protocol_min": "",
		"tls_protocol_max": "",
		"tls_cipher_suite": "",

		"attrusername": "cn",  # LDAP attribute that should be used as a username, e.g. `uid` or `sAMAccountName`
	}


	def __init__(self, provider_id, config_section_name, proactor_svc):
		super().__init__(provider_id, config_section_name)

		# This provider is heavilly using proactor design pattern to allow
		# synchronous library (python-ldap) to be used from asynchronous code
		self.ProactorService = proactor_svc

		self.LdapUri = self.Config["uri"]
		self.Base = self.Config["base"]
		self.AttrList = _prepare_attributes(self.Config)

		# Fields to filter by when locating a user
		self.IdentFields = ["mail", "mobile"]
		# If attrusername field is not empty, locate by it too
		if len(self.Config["attrusername"]) > 0:
			self.IdentFields.append(self.Config["attrusername"])


	async def get(self, credentials_id, include=None) -> Optional[dict]:
		if not credentials_id.startswith(self.Prefix):
			raise KeyError("Credentials {!r} not found".format(credentials_id))
		return await self.ProactorService.execute(self._get_worker, credentials_id, include)


	async def search(self, filter: dict = None, **kwargs) -> list:
		# TODO: Implement pagination
		filterstr = self._build_search_filter(filter)
		return await self.ProactorService.execute(self._search_worker, filterstr)


	async def count(self, filtr=None) -> int:
		filterstr = self._build_search_filter(filtr)
		return await self.ProactorService.execute(self._count_worker, filterstr)


	async def iterate(self, offset: int = 0, limit: int = -1, filtr: str = None):
		filterstr = self._build_search_filter(filtr)
		results = await self.ProactorService.execute(self._search_worker, filterstr)
		for i in results[offset : (None if limit == -1 else limit + offset)]:
			yield i


	async def locate(self, ident: str, ident_fields: dict = None, login_dict: dict = None) -> str:
		return await self.ProactorService.execute(self._locate_worker, ident, ident_fields)


	async def authenticate(self, credentials_id: str, credentials: dict) -> bool:
		return await self.ProactorService.execute(self._authenticate_worker, credentials_id, credentials)


	async def get_login_descriptors(self, credentials_id):
		# Only login with password is supported
		return [{
			"id": "default",
			"label": "Use recommended login.",
			"factors": [{
				"id": "password",
				"type": "password"
			}],
		}]


	def _get_worker(self, credentials_id, include=None) -> Optional[dict]:
		cn = base64.urlsafe_b64decode(credentials_id[len(self.Prefix):]).decode("utf-8")
		with self._ldap_client() as lc:
			try:
				results = lc.search_s(
					cn,
					ldap.SCOPE_BASE,
					filterstr=self.Config["filter"],
					attrlist=self.AttrList,
				)
			except ldap.NO_SUCH_OBJECT as e:
				raise KeyError("Credentials {!r} not found".format(credentials_id)) from e

		if len(results) > 1:
			L.exception("Multiple credentials matched ID.", struct_data={"cid": credentials_id})
			raise KeyError("Credentials {!r} not found".format(credentials_id))

		dn, entry = results[0]
		return self._normalize_credentials(dn, entry)


	def _search_worker(self, filterstr):
		# TODO: sorting
		result = []

		with self._ldap_client() as ldap_client:
			msgid = ldap_client.search(
				self.Base,
				ldap.SCOPE_SUBTREE,
				filterstr=filterstr,
				attrlist=self.AttrList,
			)
			result_iter = ldap_client.allresults(msgid)

			for res_type, res_data, res_msgid, res_controls in result_iter:
				for dn, entry in res_data:
					if dn is not None:
						result.append(self._normalize_credentials(dn, entry))

		return result


	def _count_worker(self, filterstr):
		count = 0
		with self._ldap_client() as ldap_client:
			msgid = ldap_client.search(
				self.Config["base"],
				ldap.SCOPE_SUBTREE,
				filterstr=filterstr,
				attrsonly=1,  # If attrsonly is non-zero
				attrlist=["cn", "mail", "mobile"],  # For counting, we need only absolutely minimum set of attributes
			)
			result_iter = ldap_client.allresults(msgid)

			for res_type, res_data, res_msgid, res_controls in result_iter:
				for dn, entry in res_data:
					if dn is None:
						continue
					else:
						count += 1

		return count


	def _locate_worker(self, ident: str, ident_fields: typing.Optional[typing.Mapping[str, str]]):
		# TODO: Implement configurable ident_fields support
		with self._ldap_client() as ldap_client:
			msgid = ldap_client.search(
				self.Config["base"],
				ldap.SCOPE_SUBTREE,
				filterstr=ldap.filter.filter_format(
					# Build the filter template
					# Example: (|(cn=%s)(mail=%s)(mobile=%s)(sAMAccountName=%s))
					filter_template="(|{})".format(
						"".join("({}=%s)".format(field) for field in self.IdentFields)),
					assertion_values=tuple(ident for _ in self.IdentFields)
				),
				attrlist=["cn"],
			)
			result_iter = ldap_client.allresults(msgid)
			for res_type, res_data, res_msgid, res_controls in result_iter:
				for dn, entry in res_data:
					if dn is not None:
						return self._format_credentials_id(dn)

		return None


	def _authenticate_worker(self, credentials_id: str, credentials: dict) -> bool:
		password = credentials.get("password")
		dn = base64.urlsafe_b64decode(credentials_id[len(self.Prefix):]).decode("utf-8")

		ldap_client = _LDAPObject(self.LdapUri)
		ldap_client.protocol_version = ldap.VERSION3
		ldap_client.set_option(ldap.OPT_REFERRALS, 0)

		# Enable TLS
		if self.LdapUri.startswith("ldaps"):
			self._enable_tls(ldap_client)

		try:
			ldap_client.simple_bind_s(dn, password)
		except ldap.INVALID_CREDENTIALS:
			L.log(asab.LOG_NOTICE, "Authentication failed: Invalid LDAP credentials.", struct_data={
				"cid": credentials_id, "dn": dn})
			return False

		ldap_client.unbind_s()

		return True


	def _normalize_credentials(self, dn: str, search_result: typing.Mapping):
		ret = {
			"_id": self._format_credentials_id(dn),
			"_type": self.Type,
			"_provider_id": self.ProviderID,
		}

		decoded_result = {"dn": dn}
		for k, v in search_result.items():
			if k =="userPassword":
				continue
			if isinstance(v, list):
				if len(v) == 0:
					continue
				elif len(v) == 1:
					decoded_result[k] = v[0].decode("utf-8")
				else:
					decoded_result[k] = [i.decode("utf-8") for i in v]

		v = decoded_result.pop(self.Config["attrusername"], None)
		if v is not None:
			ret["username"] = v
		else:
			# This is fallback, since we need a username on various places
			ret["username"] = dn

		v = decoded_result.pop("cn", None)
		if v is not None:
			ret["full_name"] = v

		v = decoded_result.pop("mail", None)
		if v is not None:
			ret["email"] = v

		v = decoded_result.pop("mobile", None)
		if v is not None:
			ret["phone"] = v

		v = decoded_result.pop("userAccountControl", None)
		if v is not None:
			# userAccountControl is an array of binary flags returned as a decimal integer
			# byte #1 is ACCOUNTDISABLE which corresponds to "suspended" status
			# https://learn.microsoft.com/en-us/troubleshoot/windows-server/identity/useraccountcontrol-manipulate-account-properties
			try:
				ret["suspended"] = int(v) & 2 == 2
			except ValueError:
				pass

		v = decoded_result.pop("createTimestamp", None)
		if v is not None:
			ret["_c"] = _parse_timestamp(v)
		else:
			v = decoded_result.pop("createTimeStamp", None)
			if v is not None:
				ret["_c"] = _parse_timestamp(v)

		v = decoded_result.pop("modifyTimestamp", None)
		if v is not None:
			ret["_m"] = _parse_timestamp(v)
		else:
			v = decoded_result.pop("modifyTimeStamp", None)
			if v is not None:
				ret["_m"] = _parse_timestamp(v)

		if len(decoded_result) > 0:
			ret["_ldap"] = decoded_result

		return ret


	@contextlib.contextmanager
	def _ldap_client(self):
		ldap_client = _LDAPObject(self.LdapUri)
		ldap_client.protocol_version = ldap.VERSION3
		ldap_client.set_option(ldap.OPT_REFERRALS, 0)

		network_timeout = self.Config.getint("network_timeout")
		ldap_client.set_option(ldap.OPT_NETWORK_TIMEOUT, network_timeout)

		# Enable TLS
		if self.LdapUri.startswith("ldaps"):
			self._enable_tls(ldap_client)

		ldap_client.simple_bind_s(self.Config["username"], self.Config["password"])

		try:
			yield ldap_client

		finally:
			ldap_client.unbind_s()


	def _enable_tls(self, ldap_client):
		tls_cafile = self.Config["tls_cafile"]

		# Add certificate authority
		if len(tls_cafile) > 0:
			ldap_client.set_option(ldap.OPT_X_TLS_CACERTFILE, tls_cafile)

		# Set cert policy
		if self.Config["tls_require_cert"] == "never":
			ldap_client.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)
		elif self.Config["tls_require_cert"] == "demand":
			ldap_client.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND)
		elif self.Config["tls_require_cert"] == "allow":
			ldap_client.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_ALLOW)
		elif self.Config["tls_require_cert"] == "hard":
			ldap_client.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_HARD)
		else:
			L.error("Invalid 'tls_require_cert' value: {!r}. Defaulting to 'demand'.".format(
				self.Config["tls_require_cert"]
			))
			ldap_client.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND)

		# Misc TLS options
		tls_protocol_min = self.Config["tls_protocol_min"]
		if tls_protocol_min != "":
			if tls_protocol_min not in _TLS_VERSION:
				raise ValueError("'tls_protocol_min' must be one of {} or empty.".format(list(_TLS_VERSION)))
			ldap_client.set_option(ldap.OPT_X_TLS_PROTOCOL_MIN, _TLS_VERSION[tls_protocol_min])

		tls_protocol_max = self.Config["tls_protocol_max"]
		if tls_protocol_max != "":
			if tls_protocol_max not in _TLS_VERSION:
				raise ValueError("'tls_protocol_max' must be one of {} or empty.".format(list(_TLS_VERSION)))
			ldap_client.set_option(ldap.OPT_X_TLS_PROTOCOL_MAX, _TLS_VERSION[tls_protocol_max])

		if self.Config["tls_cipher_suite"] != "":
			ldap_client.set_option(ldap.OPT_X_TLS_CIPHER_SUITE, self.Config["tls_cipher_suite"])

		# NEWCTX needs to be the last option, because it applies all the prepared options to the new context
		ldap_client.set_option(ldap.OPT_X_TLS_NEWCTX, 0)


	def _format_credentials_id(self, dn):
		return self.Prefix + base64.urlsafe_b64encode(dn.encode("utf-8")).decode("ascii")


	def _build_search_filter(self, filtr: typing.Optional[str] = None):
		if not filtr:
			filterstr = self.Config["filter"]
		else:
			# The query filter is the intersection of the filter from config
			# and the filter defined by the search request
			# The username must START WITH the given filter string
			filter_template = "(&{}({}=*%s*))".format(self.Config["filter"], self.Config["attrusername"])
			assertion_values = ["{}".format(filtr.lower())]
			filterstr = ldap.filter.filter_format(
				filter_template=filter_template,
				assertion_values=assertion_values
			)
		return filterstr


class _LDAPObject(ldap.ldapobject.LDAPObject, ldap.resiter.ResultProcessor):
	pass


def _parse_timestamp(ts: str) -> datetime.datetime:
	try:
		return datetime.datetime.strptime(ts, r"%Y%m%d%H%M%SZ")
	except ValueError:
		pass

	return datetime.datetime.strptime(ts, r"%Y%m%d%H%M%S.%fZ")


def _prepare_attributes(config: typing.Mapping):
	attr = set(config["attributes"].split(" "))
	attr.add("createTimestamp")
	attr.add("modifyTimestamp")
	attr.add("cn")
	attr.add(config["attrusername"])
	return list(attr)
