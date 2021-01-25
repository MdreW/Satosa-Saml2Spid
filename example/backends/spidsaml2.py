import logging
import saml2
import satosa.util as util

from saml2 import BINDING_HTTP_REDIRECT, BINDING_HTTP_POST
from saml2.authn_context import requested_authn_context
from saml2.metadata import entity_descriptor, sign_entity_descriptor
from saml2.saml import NAMEID_FORMAT_TRANSIENT
from saml2.sigver import security_context
from saml2.validate import valid_instance
from satosa.backends.saml2 import SAMLBackend
from satosa.context import Context
from satosa.exception import SATOSAAuthenticationError, SATOSAStateError
from satosa.response import SeeOther, Response
from satosa.saml_util import make_saml_response
from six import text_type

from . spidsaml2_validator import Saml2ResponseValidator

logger = logging.getLogger(__name__)


class SpidSAMLBackend(SAMLBackend):
    """
    A saml2 backend module (acting as a SPID SP).
    """
    _authn_context = 'https://www.spid.gov.it/SpidL1'

    def _metadata_endpoint(self, context):
        """
        Endpoint for retrieving the backend metadata
        :type context: satosa.context.Context
        :rtype: satosa.response.Response

        :param context: The current context
        :return: response with metadata
        """
        logger.debug("Sending metadata response")
        conf = self.sp.config
        metadata = entity_descriptor(conf)
        # creare gli attribute_consuming_service
        cnt = 0
        for attribute_consuming_service in metadata.spsso_descriptor.attribute_consuming_service:
            attribute_consuming_service.index = str(cnt)
            cnt += 1

        cnt = 0
        for assertion_consumer_service in metadata.spsso_descriptor.assertion_consumer_service:
            assertion_consumer_service.is_default = 'true' if not cnt else ''
            assertion_consumer_service.index = str(cnt)
            cnt += 1

        # nameformat patch... tutto questo non rispecchia gli standard OASIS
        for reqattr in metadata.spsso_descriptor.attribute_consuming_service[0].requested_attribute:
            reqattr.name_format = None
            reqattr.friendly_name = None

        # attribute consuming service service name patch
        service_name = metadata.spsso_descriptor.attribute_consuming_service[0].service_name[0]
        service_name.lang = 'it'
        service_name.text = metadata.entity_id

        # remove extension disco and uuinfo (spid-testenv2)
        #metadata.spsso_descriptor.extensions = []

        # metadata signature
        secc = security_context(conf)
        #
        sign_dig_algs = self.get_kwargs_sign_dig_algs()
        eid, xmldoc = sign_entity_descriptor(metadata, None, secc, **sign_dig_algs)

        valid_instance(eid)
        return Response(text_type(xmldoc).encode('utf-8'),
                        content="text/xml; charset=utf8")


    def get_kwargs_sign_dig_algs(self):
        kwargs = {}
        # backend support for selectable sign/digest algs
        alg_dict = dict(signing_algorithm = 'sign_alg',
						digest_algorithm = 'digest_alg')
        for alg in alg_dict:
            selected_alg = self.config['sp_config']['service']['sp'].get(alg)
            if not selected_alg: continue
            kwargs[alg_dict[alg]] = selected_alg
        return kwargs


    def check_blacklist(self):
        # If IDP blacklisting is enabled and the selected IDP is blacklisted,
        # stop here
        if self.idp_blacklist_file:
            with open(self.idp_blacklist_file) as blacklist_file:
                blacklist_array = json.load(blacklist_file)['blacklist']
                if entity_id in blacklist_array:
                    logger.debug(f"IdP with EntityID {entity_id} is blacklisted")
                    raise SATOSAAuthenticationError(
                        context.state, 
                        "Selected IdP is blacklisted for this backend"
                    )


    def authn_request(self, context, entity_id):
        """
        Do an authorization request on idp with given entity id.
        This is the start of the authorization.

        :type context: satosa.context.Context
        :type entity_id: str
        :rtype: satosa.response.Response

        :param context: The current context
        :param entity_id: Target IDP entity id
        :return: response to the user agent
        """
        self.check_blacklist()

        kwargs = {}
        # fetch additional kwargs
        kwargs.update(self.get_kwargs_sign_dig_algs())

        authn_context = self.construct_requested_authn_context(entity_id)
        requested_authn_context = authn_context or requested_authn_context(class_ref=self._authn_context)

        # force_auth = true only if SpidL >= 2
        if 'SpidL1' in authn_context.authn_context_class_ref[0].text:
            force_authn = 'false'
        else:
            force_authn = 'true'

        try:
            binding = saml2.BINDING_HTTP_POST
            destination = context.request['entityID']
            client = self.sp

            logger.debug(f"binding: {binding}, destination: {destination}")

            # acs_endp, response_binding = self.sp.config.getattr("endpoints", "sp")["assertion_consumer_service"][0]
            # req_id, req = self.sp.create_authn_request(
                # destination, binding=response_binding, **kwargs)

            logger.debug(f'Redirecting user to the IdP via {binding} binding.')
            # use the html provided by pysaml2 if no template was specified or it didn't exist

            # SPID want the fqdn of the IDP as entityID, not the SSO endpoint
            # dovrebbe essere destination ma nel caso di spid-testenv2 è entityid...
            # binding, destination = self.sp.pick_binding("single_sign_on_service", None, "idpsso", entity_id=entity_id)
            # location = client.sso_location(destination, binding)
            location = client.sso_location(entity_id, binding)
            location_fixed = entity_id
            # ...hope to see the SSO endpoint soon in spid-testenv2
            # fixed: https://github.com/italia/spid-testenv2/commit/6041b986ec87ab8515dd0d43fed3619ab4eebbe9

            # verificare qui
            # acs_endp, response_binding = self.sp.config.getattr("endpoints", "sp")["assertion_consumer_service"][0]

            authn_req = saml2.samlp.AuthnRequest()
            authn_req.force_authn = force_authn
            authn_req.destination = location
            # spid-testenv2 preleva l'attribute consumer service dalla authnRequest (anche se questo sta già nei metadati...)
            authn_req.attribute_consuming_service_index = "0"

            # breakpoint()
            issuer = saml2.saml.Issuer()
            issuer.name_qualifier = client.config.entityid
            issuer.text = client.config.entityid
            issuer.format = "urn:oasis:names:tc:SAML:2.0:nameid-format:entity"
            authn_req.issuer = issuer

            # message id
            authn_req.id = saml2.s_utils.sid()
            authn_req.version = saml2.VERSION # "2.0"
            authn_req.issue_instant = saml2.time_util.instant()

            name_id_policy = saml2.samlp.NameIDPolicy()
            # del(name_id_policy.allow_create)
            name_id_policy.format = NAMEID_FORMAT_TRANSIENT
            authn_req.name_id_policy  = name_id_policy

            # TODO: use a parameter instead
            authn_req.requested_authn_context = requested_authn_context
            authn_req.protocol_binding = binding

            assertion_consumer_service_url = client.config._sp_endpoints['assertion_consumer_service'][0][0]
            authn_req.assertion_consumer_service_url = assertion_consumer_service_url #'http://sp-fqdn/saml2/acs/'

            authn_req_signed = client.sign(authn_req, sign_prepare=False,
                                           sign_alg=kwargs['sign_alg'],
                                           digest_alg=kwargs['digest_alg'])
            session_id = authn_req.id

            _req_str = authn_req_signed
            logger.debug('AuthRequest to {}: {}'.format(destination, (_req_str)))

            relay_state = util.rndstr()
            ht_args = client.apply_binding(binding,
                                           _req_str, location,
                                           sign=True,
                                           sigalg=kwargs['sign_alg'],
                                           relay_state=relay_state)


            if self.sp.config.getattr('allow_unsolicited', 'sp') is False:
                if authn_req.id in self.outstanding_queries:
                    errmsg = "Request with duplicate id {}".format(req_id)
                    logger.debug(errmsg)
                    raise SATOSAAuthenticationError(context.state, errmsg)
                self.outstanding_queries[authn_req.id] = authn_req_signed

            context.state[self.name] = {"relay_state": relay_state}

            logger.debug("ht_args: %s" % ht_args)
            return make_saml_response(binding, ht_args)

        except Exception as exc:
            logger.debug("Failed to construct the AuthnRequest for state")
            raise SATOSAAuthenticationError(context.state, "Failed to construct the AuthnRequest") from exc


    def authn_response(self, context, binding):
        """
        Endpoint for the idp response
        :type context: satosa.context,Context
        :type binding: str
        :rtype: satosa.response.Response

        :param context: The current context
        :param binding: The saml binding type
        :return: response
        """
        if not context.request["SAMLResponse"]:
            logger.debug("Missing Response for state")
            raise SATOSAAuthenticationError(context.state, "Missing Response")

        try:
            authn_response = self.sp.parse_authn_request_response(
                context.request["SAMLResponse"],
                binding, outstanding=self.outstanding_queries)
        except Exception as err:
            logger.debug("Failed to parse authn request for state")
            raise SATOSAAuthenticationError(context.state, "Failed to parse authn request") from err

        if self.sp.config.getattr('allow_unsolicited', 'sp') is False:
            req_id = authn_response.in_response_to
            if req_id not in self.outstanding_queries:
                errmsg = "No request with id: {}".format(req_id),
                logger.debug(errmsg)
                raise SATOSAAuthenticationError(context.state, errmsg)
            del self.outstanding_queries[req_id]
        
        # Context validation
        if not context.state.get(self.name):
            _msg = "context.state[self.name] KeyError: where self.name is {}".format(self.name)
            logger.error(_msg)
            raise SATOSAStateError(context.state, _msg)
        if not context.state.get('Saml2IDP'):
            _msg = "context.state['Saml2IDP'] KeyError"
            logger.error(_msg)
            raise SATOSAStateError(context.state, "State without Saml2IDP")

        # check if the relay_state matches the cookie state
        if context.state[self.name]["relay_state"] != context.request["RelayState"]:
            logger.debug("State did not match relay state for state")
            raise SATOSAAuthenticationError(context.state, "State did not match relay state")

        # Spid and SAML2 additional tests
        issuer = context.state['Saml2IDP']['resp_args']['sp_entity_id']
        accepted_time_diff = self.config['sp_config']['accepted_time_diff']
        recipient = self.config['sp_config']['service']['sp']['endpoints']['assertion_consumer_service'][0][0]
        authn_context_classref = self.config['acr_mapping']['']
        in_response_to = context.state['Saml2IDP']['resp_args']['in_response_to']

        validator = Saml2ResponseValidator(authn_response=authn_response.xmlstr,
                                           recipient = recipient,
                                           in_response_to=in_response_to,
                                           accepted_time_diff = accepted_time_diff,
                                           authn_context_class_ref=authn_context_classref)
        validator.run()

        context.decorate(Context.KEY_BACKEND_METADATA_STORE, self.sp.metadata)
        if self.config.get(SAMLBackend.KEY_MEMORIZE_IDP):
            issuer = authn_response.response.issuer.text.strip()
            context.state[Context.KEY_MEMORIZED_IDP] = issuer
        context.state.pop(self.name, None)
        context.state.pop(Context.KEY_FORCE_AUTHN, None)
        return self.auth_callback_func(context, self._translate_response(authn_response, context.state))
