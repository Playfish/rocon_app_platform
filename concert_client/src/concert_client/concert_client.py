#!/usr/bin/env python
#
# License: BSD
#   https://raw.github.com/robotics-in-concert/rocon_app_manager/concert_client/LICENSE
#
##############################################################################
# Imports
##############################################################################

import roslib
roslib.load_manifest('concert_client')
import rospy

from rocon_hub_client.hub_client import HubClient
from .concertmaster_discovery import ConcertMasterDiscovery
import concert_msgs.srv as concert_srvs
import appmanager_msgs.srv as appmanager_srvs
import gateway_msgs.msg as gateway_msgs
import gateway_msgs.srv as gateway_srvs
from .util import createRule, createRemoteRule

##############################################################################
# Concert Client
##############################################################################


class ConcertClient(object):
    concertmaster_key = "concertmasterlist"

    is_connected = False
    is_invited = False
    hub_client = None

    gateway = None
    gateway_srv = {}

    concertmasterlist = []

    invitation_srv = 'invitation'
    status_srv = 'status'

    def __init__(self):
        self.is_connected = False
        self.name = rospy.get_name()
        self.param = self.setupRosParameters()

        self.hub_client = HubClient(whitelist=self.param['hub_whitelist'],
                                    blacklist=self.param['hub_blacklist'],
                                    is_zeroconf=False,
                                    namespace='rocon',
                                    name=self.name,
                                    callbacks=None)

        self.masterdiscovery = ConcertMasterDiscovery(self.hub_client, self.concertmaster_key, self.processNewMaster)

        self.gateway_srv = {}
        self.gateway_srv['gateway_info'] = rospy.ServiceProxy('~gateway_info', gateway_srvs.GatewayInfo)
        self.gateway_srv['flip'] = rospy.ServiceProxy('~flip', gateway_srvs.Remote)
        try:
            self.gateway_srv['gateway_info'].wait_for_service()
            self.gateway_srv['flip'].wait_for_service()
        except rospy.exceptions.ROSInterruptException:
            rospy.logerr("Concert Client : interrupted while waiting for gateway services to appear.")
            return

        self.appmanager_srv = {}
        self.appmanager_srv['init'] = rospy.ServiceProxy('~init', appmanager_srvs.Init)
        self.appmanager_srv['apiflip_request'] = rospy.ServiceProxy('~apiflip_request', appmanager_srvs.FlipRequest)
        self.appmanager_srv['invitation'] = rospy.ServiceProxy('~relay_invitation', concert_srvs.Invitation)
        self.appmanager_srv['init'].wait_for_service()

    def spin(self):
        self.connectToHub()
        rospy.loginfo("Concert Client: connected to Hub [%s]" % self.hub_uri)
        rospy.loginfo("Concert Client; scanning for concerts...")
        self.startMasterDiscovery()
        rospy.spin()
        self.leaveMasters()

    def connectToHub(self):
        while not rospy.is_shutdown() and not self.is_connected:
            rospy.loginfo("Getting Hub info from gateway...")
            gateway_info = self.gateway_srv['gateway_info']()
            if gateway_info.connected == True:
                hub_uri = gateway_info.hub_uri
                if self.hub_client.connect(hub_uri):
                    self.init(gateway_info.name, hub_uri)
            else:
                rospy.loginfo("No hub is available. Try later")
            rospy.sleep(1.0)

    def init(self, name, uri):
        '''
        @param name : the unique gateway name
        @type string
        @param uri : the hub uri
        @type string
        '''
        self.is_connected = True
        self.name = name
        self.hub_uri = uri

        self.service = {}
        self.service['invitation'] = rospy.Service(self.name + '/' + self.invitation_srv, concert_srvs.Invitation, self.processInvitation)
        self.service['status'] = rospy.Service(self.name + '/' + self.status_srv, concert_srvs.Status, self.processStatus)
        self.master_services = ['/' + self.name + '/' + self.invitation_srv, '/' + self.name + '/' + self.status_srv]

        app_init_req = appmanager_srvs.InitRequest(name)
        rospy.loginfo("Concert Client : initialising the app manager [%s]" % name)
        unused_resp = self.appmanager_srv['init'](app_init_req)

    def setupRosParameters(self):
        param = {}
        param['hub_whitelist'] = ''
        param['hub_blacklist'] = ''
        param['cm_whitelist'] = []
        param['cm_blacklist'] = []

        return param

    def startMasterDiscovery(self):
        self.masterdiscovery.start()

    def processNewMaster(self, discovered_masterlist):
        # find newly discovered masters
        new_masters = [m for m in discovered_masterlist if m not in self.concertmasterlist]
        self.concertmasterlist += new_masters

        for master in new_masters:
            self.joinMaster(master)

        # cleaning gone masters
        self.concertmasterlist = [m for m in self.concertmasterlist and discovered_masterlist]

    def joinMaster(self, master):
        self.flips(master, self.master_services, gateway_msgs.ConnectionType.SERVICE, True)

        req = appmanager_srvs.FlipRequestRequest(master, True)
        resp = self.appmanager_srv['apiflip_request'](req)
        if resp.result == False:
            rospy.logerr("Concert Client : failed to flip appmanager APIs")

    def leaveMasters(self):
        self.masterdiscovery.set_stop()

        try:
            for master in self.concertmasterlist:
                self._leave_master(master)
        except Exception as unused_e:
            rospy.logdebug("Concert Client: gateway already down, no shutdown work required.")

    def _leave_master(self, master):
        self.flips(master, self.master_services, gateway_msgs.ConnectionType.SERVICE, False)
        req = appmanager_srvs.FlipRequestRequest(master, False)
        resp = self.appmanager_srv['apiflip_request'](req)
        if resp.result == False:
            self.logerr("Failed to Flip Appmanager APIs")

    def processInvitation(self, req):
        cm_name = req.name

        # Check if concert master is in white list
        if cm_name in self.param['cm_whitelist']:
            return self.acceptInvitation(req)
        elif len(self.param['cm_whitelist']) == 0 and cm_name not in self.param['cm_blacklist']:
            return self.acceptInvitation(req)
        else:
            return concert_srvs.InvitationResponse(False)

    def acceptInvitation(self, req):
        rospy.loginfo("Concert Client : accepting invitation from %s" % req.name)
        resp = self.appmanager_srv['invitation'](req)

        return resp

    def processStatus(self, req):
        resp = concert_srvs.StatusResponse()
        resp.status = "free-agent" if not self.is_invited else "busy"
        return resp

    def flips(self, remote_name, topics, type, ok_flag):
        if len(topics) == 0:
            return
        req = gateway_srvs.RemoteRequest()
        req.cancel = not ok_flag
        req.remotes = []
        for t in topics:
            req.remotes.append(createRemoteRule(remote_name, createRule(t, type)))

        resp = self.gateway_srv['flip'](req)

        if resp.result == 0:
            rospy.loginfo("Concert Client : successfully flipped to the concert %s" % str(topics))
        else:
            rospy.logerr("Concert Client : failed to flip [%s][%s]" % (str(topics), str(resp.error_message)))