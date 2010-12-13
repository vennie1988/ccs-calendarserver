# -*- test-case-name: calendarserver.provision.test.test_root -*-
##
# Copyright (c) 2005-2010 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

__all__ = [
    "RootResource",
]

from twext.python.log import Logger
from twext.web2 import responsecode
from twext.web2.auth.wrapper import UnauthorizedResponse
from twext.web2.dav import davxml
from twext.web2.http import HTTPError, StatusResponse

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.web.xmlrpc import Proxy

from twistedcaldav.config import config
from twistedcaldav.extensions import DAVFile, CachingPropertyStore
from twistedcaldav.extensions import DirectoryPrincipalPropertySearchMixIn
from twistedcaldav.extensions import ReadOnlyResourceMixIn
from twistedcaldav.resource import CalDAVComplianceMixIn

log = Logger()


class RootResource (ReadOnlyResourceMixIn, DirectoryPrincipalPropertySearchMixIn, CalDAVComplianceMixIn, DAVFile):
    """
    A special root resource that contains support checking SACLs
    as well as adding responseFilters.
    """

    useSacls = False

    # Mapping of top-level resource paths to SACLs.  If a request path
    # starts with any of these, then the list of SACLs are checked.  If the
    # request path does not start with any of these, then no SACLs are checked.
    saclMap = {
        "addressbooks" : ("addressbook",),
        "calendars" : ("calendar",),
        "directory" : ("addressbook",),
        "principals" : ("addressbook", "calendar"),
        "webcal" : ("calendar",),
    }

    def __init__(self, path, *args, **kwargs):
        super(RootResource, self).__init__(path, *args, **kwargs)

        if config.EnableSACLs:
            if RootResource.CheckSACL:
                self.useSacls = True
            else:
                log.warn("SACLs are enabled, but SACLs are not supported.")

        self.contentFilters = []

        if config.ResponseCompression:
            from twext.web2.filter import gzip
            self.contentFilters.append((gzip.gzipfilter, True))

        if not config.EnableKeepAlive:
            def addConnectionClose(request, response):
                response.headers.setHeader("connection", ("close",))
                request.chanRequest.channel.setReadPersistent(False)
                return response
            self.contentFilters.append((addConnectionClose, True))

    def deadProperties(self):
        if not hasattr(self, "_dead_properties"):
            # Get the property store from super
            deadProperties = super(RootResource, self).deadProperties()

            # Wrap the property store in a memory store
            deadProperties = CachingPropertyStore(deadProperties)

            self._dead_properties = deadProperties

        return self._dead_properties

    def defaultAccessControlList(self):
        return config.RootResourceACL

    @inlineCallbacks
    def checkSacl(self, request):
        """
        Check SACLs against the current request
        """

        topLevel = request.path.strip("/").split("/")[0]
        saclServices = self.saclMap.get(topLevel, None)
        if not saclServices:
            returnValue(True)

        try:
            authnUser, authzUser = yield self.authenticate(request)
        except Exception:
            response = (yield UnauthorizedResponse.makeResponse(
                request.credentialFactories,
                request.remoteAddr
            ))
            raise HTTPError(response)


        # SACLs are enabled in the plist, but there may not actually
        # be a SACL group assigned to this service.  Let's see if
        # unauthenticated users are allowed by calling CheckSACL
        # with an empty string.
        if authzUser == davxml.Principal(davxml.Unauthenticated()):
            for saclService in saclServices:
                if RootResource.CheckSACL("", saclService) == 0:
                    # No group actually exists for this SACL, so allow
                    # unauthenticated access
                    returnValue(True)
            # There is a SACL group for at least one of the SACLs, so no
            # unauthenticated access
            response = (yield UnauthorizedResponse.makeResponse(
                request.credentialFactories,
                request.remoteAddr
            ))
            raise HTTPError(response)

        # Cache the authentication details
        request.authnUser = authnUser
        request.authzUser = authzUser

        # Figure out the "username" from the davxml.Principal object
        request.checkingSACL = True

        for collection in self.principalCollections():
            principal = collection._principalForURI(authzUser.children[0].children[0].data)
            if principal is None:
                response = (yield UnauthorizedResponse.makeResponse(
                    request.credentialFactories,
                    request.remoteAddr
                ))
                raise HTTPError(response)

        delattr(request, "checkingSACL")
        username = principal.record.shortNames[0]

        access = False
        for saclService in saclServices:
            if RootResource.CheckSACL(username, saclService) == 0:
                # Access is allowed
                access = True
                break

        # Mark SACLs as having been checked so we can avoid doing it
        # multiple times
        request.checkedSACL = True

        if access:
            returnValue(True)

        log.info("User %r is not enabled with the %r SACL" % (username, saclServices,))
        raise HTTPError(responsecode.FORBIDDEN)

    @inlineCallbacks
    def locateChild(self, request, segments):

        for filter in self.contentFilters:
            request.addResponseFilter(filter[0], atEnd=filter[1])

        # Examine cookies for wiki auth token; if there, ask the paired wiki
        # server for the corresponding record name.  If that maps to a
        # principal, assign that to authnuser.

        if not hasattr(request, "checkedWiki"):
            # Only do this once per request
            request.checkedWiki = True

            wikiConfig = config.Authentication.Wiki
            cookies = request.headers.getHeader("cookie")
            if wikiConfig["Enabled"] and cookies is not None:
                for cookie in cookies:
                    if cookie.name == wikiConfig["Cookie"]:
                        token = cookie.value
                        break
                else:
                    token = None

                if token is not None and token != "unauthenticated":
                    log.debug("Wiki sessionID cookie value: %s" % (token,))
                    proxy = Proxy(wikiConfig["URL"])
                    try:
                        username = (yield proxy.callRemote(wikiConfig["UserMethod"], token))
                    except Exception, e:
                        log.error("Failed to look up wiki token (%s)" % (e,))
                        username = None

                    if username is not None:
                        log.debug("Wiki lookup returned user: %s" % (username,))
                        principal = None
                        directory = request.site.resource.getDirectory()
                        record = directory.recordWithShortName("users", username)
                        log.debug("Wiki user record for user %s : %s" % (username, record))
                        if record:
                            # Note: record will be None if it's a /Local/Default user
                            for collection in self.principalCollections():
                                principal = collection.principalForRecord(record)
                                if principal is not None:
                                    break

                        if principal:
                            log.debug("Wiki-authenticated principal %s being assigned to authnUser and authzUser" % (record.guid,))
                            request.authzUser = request.authnUser = davxml.Principal(
                                davxml.HRef.fromString("/principals/__uids__/%s/" % (record.guid,))
                            )

        # We don't want the /inbox resource to pay attention to SACLs because
        # we just want it to use the hard-coded ACL for the imip reply user.
        # The /timezones resource is used by the wiki web calendar, so open
        # up that resource.
        if segments[0] in ("inbox", "timezones"):
            request.checkedSACL = True

        elif (len(segments) > 2 and (segments[1] == "wikis" or
            (segments[1] == "__uids__" and segments[2].startswith("wiki-")))):

            # This is a wiki-related resource. SACLs are not checked.
            request.checkedSACL = True

            # The authzuser value is set to that of the wiki principal if
            # not already set.
            if not hasattr(request, "authzUser"):
                wikiName = None
                if segments[1] == "wikis":
                    wikiName = segments[2]
                else:
                    wikiName = segments[2][5:]
                if wikiName:
                    log.debug("Wiki principal %s being assigned to authzUser" % (wikiName,))
                    request.authzUser = davxml.Principal(
                        davxml.HRef.fromString("/principals/wikis/%s/" % (wikiName,))
                    )

        elif self.useSacls and not hasattr(request, "checkedSACL") and not hasattr(request, "checkingSACL"):
            yield self.checkSacl(request)

        if config.RejectClients:
            #
            # Filter out unsupported clients
            #
            agent = request.headers.getHeader("user-agent")
            if agent is not None:
                for reject in config.RejectClients:
                    if reject.search(agent) is not None:
                        log.info("Rejecting user-agent: %s" % (agent,))
                        raise HTTPError(StatusResponse(
                            responsecode.FORBIDDEN,
                            "Your client software (%s) is not allowed to access this service." % (agent,)
                        ))

        # Look for forwarding
        if config.Partitioning.Enabled:
            remote_ip = request.headers.getRawHeaders('x-forwarded-for')
            if remote_ip and len(remote_ip) == 1:
                request.forwarded_for = remote_ip[0]
                if not hasattr(request, "extendedLogItems"):
                    request.extendedLogItems = {}
                request.extendedLogItems["xff"] = remote_ip[0]

        child = (yield super(RootResource, self).locateChild(request, segments))
        returnValue(child)

    def http_COPY       (self, request): return responsecode.FORBIDDEN
    def http_MOVE       (self, request): return responsecode.FORBIDDEN
    def http_DELETE     (self, request): return responsecode.FORBIDDEN

# So CheckSACL will be parameterized
# We do this after RootResource is defined
try:
    from calendarserver.platform.darwin._sacl import CheckSACL
    RootResource.CheckSACL = CheckSACL
except ImportError:
    RootResource.CheckSACL = None
