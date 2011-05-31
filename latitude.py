#!/usr/bin/python

################################################################################
# Configuration
#

# System modules
import argparse    # python-argparse
import gobject
import time
import os
import sys
import logging
import signal

# WIFI scanning
import subprocess

# Skyhook
import re
import socket
import httplib

# Geolocation
import location    # python-location

# Network connectivity
import conic

# Google Latitude
import httplib2
import pickle
from apiclient.discovery import build   # google-api-python-client
from apiclient.oauth import FlowThreeLegged
from apiclient.ext.authtools import run
from apiclient.ext.file import Storage

# Device wrapper
import osso

# Definitions
UPDATE_AT_MOST    = 1      # NEVER update more than this (minutes) even when moving
UPDATE_AT_LEAST   = 15     # NEVER update LESS than this (minutes) even when still (to avoid "stale points" in Latitude)
TIMEOUT_CONN      = 10     # How long we are allowed to wait for a connection
TIMEOUT_GPS       = 30     # How long we are allowed to wait for a GPS fix
TIMEOUT_GSM       = 10     # How lone we are allowed to wait for a GSM fix
MIN_ACCURACY_GSM  = 2500   # The minimal accuracy of a cell fix to be accepted
MIN_ACCURACY_GPS  = 150    # The minimal accuracy of a gps fix to be accepted


#
# Auxiliary
#

class Location:
    # Member data
    lat=0
    lng=0
    alt=0
    acc=9999
    alt=0
    altacc=0
    head=0
    speed=0
    time=0
    
    def getData(self):
        data = {
            "data": {
              "kind": "latitude#location",
              "latitude": self.lat,
              "longitude": self.lng,
              "accuracy": self.acc,
              "timestampMs":int(self.time*1000),
              #"altitude": self.alt,
              #"altitudeAccuracy": self.altacc,
              #"heading": self.head,
              #"speed": self.speed
              }
        }
        return data

class ServiceWrapper:
    # Member data
    logger = logging.getLogger('ServiceWrapper')
    service = None
    
    # Constructor
    def __init__(self):        
        # Connect to Latitude
        storage = Storage('latitude.dat')
        credentials = storage.get()
        if credentials is None or credentials.invalid == True:
            auth_discovery = build("latitude", "v1").auth_discovery()
            flow = FlowThreeLegged(auth_discovery,
                # You MUST have a consumer key and secret tied to a
                # registered domain to use the latitude API.
                #
                # https://www.google.com/accounts/ManageDomains
                consumer_key='maleadt.be',
                consumer_secret='Yh0qdTQ-pHGQiyguyINr64WK',
                user_agent='efficient-latitude/0.1',
                domain='maleadt.be',
                scope='https://www.googleapis.com/auth/latitude',
                xoauth_displayname='Efficient Latitude',
                location='all',
                granularity='best'
                )
            # Work around https://code.google.com/p/google-api-python-client/issues/detail?id=34
            while len(sys.argv) > 1:
                sys.argv.pop()
            credentials = run(flow, storage)
            if credentials is None or credentials.invalid == True:
                raise Exception("Invalid Latitude credentials")
        http = httplib2.Http()
        http = credentials.authorize(http)
        self.service = build("latitude", "v1", http=http)
    
    # Actions
    def upload(self, entries):
        for entry in entries:
            self.service.location().insert(body = entry.getData()).execute()

class DeviceWrapper(gobject.GObject):
    # Member data
    logger = logging.getLogger('DeviceWrapper')
    context = osso.Context("osso_test_device_on", "0.0.1", False)
    device = osso.DeviceState(context)
    
    # Constructor
    def __init__(self):
        gobject.GObject.__init__(self)
        
        # Register callbacks
        self.device.set_device_state_callback(self.cbState)
    
    # Events
    def cbState(shutdown, save_unsaved_data, memory_low, system_inactivity, message):        
        logger.debug("Shutdown: ", shutdown)
        logger.debug("Save unsaved data: ", save_unsaved_data)
        logger.debug("Memory low: ", memory_low)
        logger.debug("System Inactivity: ", system_inactivity)
        logger.debug("Message: ", message)

class ConnectionWrapper(gobject.GObject):
    # Signals
    __gsignals__ = {
        "connected": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE, ()),
        "disconnected": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE, ()),
    }
    
    # Member data
    logger = logging.getLogger('ConnectionWrapper')
    connected = False
    connection = conic.Connection()
    
    # Constructor
    def __init__(self):
        gobject.GObject.__init__(self)
        
        # Listen for events
        self.connection.connect("connection-event", self.on_connection_event)
        self.connection.set_property("automatic-connection-events", True)
    
    # Events
    def on_connection_event(self, connection, event):    
        status = event.get_status()
        bearer = event.get_bearer_type()
        
        if status == conic.STATUS_CONNECTED:
            self.logger.debug("Device connected to %s" % bearer)
            self.connected = True
            self.emit("connected")
        elif status == conic.STATUS_DISCONNECTED:
            self.logger.debug("Device disconnected from %s" % bearer)
            self.connected = False
            self.emit("disconnected")
    
    # Actions
    def request(self):
        self.connection.request_connection(conic.CONNECT_FLAG_NONE)

class Skyhook():
    # Member data    
    logger = logging.getLogger('Skyhook')
    
    # Constructor
    def __init__(self, bssid):
        self.apihost="api.skyhookwireless.com"
        self.url="/wps2/location"
        self.bssid=self._validateBssid(bssid)
        self.results={}
        self.reqStr = """<?xml version='1.0'?>
            <LocationRQ xmlns='http://skyhookwireless.com/wps/2005' version='2.6' street-address-lookup='full'>
              <authentication version='2.0'>
                <simple>skyhook
                  <username>beta</username>
                  <realm>js.loki.com</realm>
                </simple>
              </authentication>
              <access-point>
                <mac>%s</mac>
                <signal-strength>-50</signal-strength>
              </access-point>
            </LocationRQ>""" % self.bssid

    def _validateBssid(self, bssid):
        if not re.compile(r"^([\dabcdef]{2}:){5}[\dabcdef]{2}$", re.I).search(bssid):
            raise Exception("BSSID [%s] does not appear to be valid" % bssid)
        bssid = bssid.replace(":", "").upper()
        return bssid

    def _parseResponse(self, xml):
        match = re.compile(r"<latitude>([^<]*)</latitude><longitude>([^<]*)</longitude>").search(xml)
        if match:
            self.results["Latitude"] = match.group(1)
            self.results["Longitude"] = match.group(2)
        else:
            raise Exception("Couldn't find basic attributes in response.")

    def getLocation(self):
        try:
            dataLen=len(self.reqStr)
            conn = httplib.HTTPSConnection(self.apihost)
            conn.putrequest("POST", self.url)
            conn.putheader("Content-type", "text/xml")
            conn.putheader("Content-Length", str(dataLen))
            conn.endheaders()
            conn.send(self.reqStr)
        except (socket.gaierror, socket.error):
            raise Exception("There was a problem when connecting to host [%s]" % (self.apihost))

        response = conn.getresponse()
        if response.status != 200:
            raise Exception("There was an error from the sever: [%s %s]" % (response.status, response.reason))

        xml = response.read()
        if re.compile(r"Unable to locate").search(xml):
            raise Exception("Unable to find info for [%s]" % bssid)

        self._parseResponse(xml)
        return self.results

class GPSWrapper(gobject.GObject):
    # Signals
    __gsignals__ = {
        "fix": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE, (object, )),
        "nofix": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE, ()),
        "start": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE, ()),
        "stop": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE, ()),
    }
    
    # Auxiliary
    class Source:
        GSM=1
        GPS=2
        WIFI=3
    class Aid:
        NONE=1
        INTERNET=2
    
    # Member data
    logger = logging.getLogger('GPSWrapper')
    control = location.GPSDControl.get_default()
    device = location.GPSDevice()
    fix_tries = 0
    running = False
    owned = False
    source = None
    aid = None
    
    # Constructor
    def __init__(self):
        gobject.GObject.__init__(self)        
        # Listen for events
        self.control.connect("gpsd-running", self.onStart)
        self.control.connect("gpsd-stopped", self.onStop)
        self.control.connect("error-verbose", self.onError)
        self.device.connect("changed", self.onChanged)
        
        self._getWIFI()
    
    # Events
    def onStart(self, control):
        self.logger.debug("Control started")
        self.running = True
        self.emit("start")
    def onStop(self, control):
        self.logger.debug("Control stopped")
        self.running = False
        self.emit("stop")
    def onError(self, control, error):
        self.logger.error("GPS error: %d" % error)
    def onChanged(self, device):        
        # If we don't start the control, we also don't get the signals. So use the fix
        # to determine whether the device is still running)
        if (not self.owned):
            if (self.device.status == location.GPS_DEVICE_STATUS_NO_FIX):
                if (self.running):
                    self.logger.debug("External GPSD stop")
                    self.onStop(self.control);
            else:
                if (not self.running):
                    self.logger.debug("External GPSD start")
                    self.onStart(self.control);
        
        # Generate a location
        mode = device.fix[0]
        newLocation = Location()
        newLocation.time = time.time()
        newLocation.lat = device.fix[4]
        newLocation.lng = device.fix[5]
        newLocation.acc = device.fix[6]/100
        newLocation.alt = device.fix[7]
        newLocation.altacc = device.fix[8]
        newLocation.head = device.fix[9]
        newLocation.speed = device.fix[11]
        self.logger.debug("Received raw location data mode %d (attempt %d): lat=%f, lon=%f (accuracy of %f) alt=%f (accuracy of %f), head=%f, speed=%f" % (mode, self.fix_tries, newLocation.lat, newLocation.lng, newLocation.acc, newLocation.alt, newLocation.altacc, newLocation.head, newLocation.speed))
                    
        # Process the changeset
        valid = False
        if self.source == self.Source.GPS:
            valid = self.processGPS(mode, newLocation)
        elif self.source == self.Source.GSM:
            valid = self.processGSM(mode, newLocation)
            # We can't be sure to cut the lookup directly short here
            # "Application might receive MCC fixes before base station
            # information from external location server is fetched and as a
            # fallback if e.g. network is temporary unavailable. "
        if valid:            
            self.logger.debug("Emitting GPS fix")
            self.emit("fix", newLocation)
    
    def processGPS(self, mode, newLocation):
        # Ignore cached or country-size measurements
        if mode < 2:
            return False

        # Skip the NaN's in accuracy
        if newLocation.acc != newLocation.acc:
            return False
            
        # I don't care about data of low accuracy, let's wait for a new fix
        if newLocation.acc > MIN_ACCURACY_GPS:
            return False
        
        # Manage invalid alttiude accuracy
        if newLocation.altacc > 32000:
            location.altacc = 0

        # Try at least three times to get a "type 3 fix"
        self.fix_tries += 1
        if mode < 3 and self.fix_tries < 3:
            return False
        self.fix_tries = 0        
        return True
    def processGSM(self, mode, newLocation):
        # Ignore cached or country-size measurements
        # TODO: howto detect MMC lookups? mode seems 2 either way
        if mode < 2:
            return False
            
        # I don't care about data of very low accuracy
        if newLocation.acc > MIN_ACCURACY_GSM:
            return False
        
        return True
    
    # Actions
    def start(self, source, aid):
        self.owned = True
        self.source = source
        self.aid = aid
        
        if (source == self.Source.GSM):
            if (aid == self.Aid.INTERNET):
                self.control.set_properties(preferred_method = location.METHOD_ACWP)
            else:
                self.control.set_properties(preferred_method = location.METHOD_CWP)
            self.control.start()
        elif (source == self.Source.GPS):
            if (aid == self.Aid.INTERNET):
                self.control.set_properties(preferred_method = location.METHOD_AGNSS)
            else:
                self.control.set_properties(preferred_method = location.METHOD_GNSS)
            self.control.start()
        elif (source == self.Source.WIFI):
            addresses = self._getWIFI()
            emits = 0
            if (aid == self.Aid.INTERNET):
                for address in addresses:
                    skyhook = Skyhook(address)
                    try:
                        result = skyhook.getLocation()
                        newLocation = Location()
                        newLocation.lat = result["Latitude"]
                        newLocation.lng = result["Longitude"]
                        newLocation.accuracy = 10
                        self.emit("fix", newLocation)
                        emits = emits + 1
                    except Exception, err:
                        self.logger.error("Could not lookup over WIFI: %s", str(err))
                        pass
            else:
                self.logger.error("Offline WIFI lookup not implemented yet")
                # TODO: implement this
            if emits == 0:
                self.emit("nofix")
                
    def stop(self):
        self.owned = False
        
        if (self.source == self.Source.GSM):
            self.control.stop()
        if (self.source == self.Source.GPS):
            self.control.stop()
    
    # Auxiliary
    def _getWIFI(self):
        proc = subprocess.Popen('iwlist scan 2>/dev/null', shell=True, stdout=subprocess.PIPE, )
        stdout_str = proc.communicate()[0]
        stdout_list=stdout_str.split('\n')
        address=[]
        for line in stdout_list:
            line=line.strip()
            match=re.search('Address: (\S+)',line)
            if match:
                address.append(match.group(1)) 
        return address

class Actor:
    # Auxiliary
    class State:
        IDLE = 0
        UPDATING_WIFI = 1
        UPDATING_GSM = 2
        UPDATING_GPS = 3
        CONNECTING = 4
    
    # Member data
    logger = logging.getLogger('Actor')
    cache = []
    state = State.IDLE
    timeout = None
    
    # Constructor
    def __init__(self):        
        # Listen for GPS events
        global gps
        gps.connect("fix", self.onFix)
        gps.connect("nofix", self.onNoFix)
        
        # Listen for connection events
        global connection
        connection.connect("connected", self.onConnected)
    
    # Events
    def onFix(self, gps, location):
        # Fill the cache
        if (len(self.cache) == 0):
            self.cache.append(location)
        elif (time.time()  - self.cache[-1].time > UPDATE_AT_MOST * 60):
            self.cache.append(location)
        elif (self.cache[-1].acc > location.acc):
            self.cache[-1] = location
        else:
            return  # TODO: this can break cell update. always schedule timeout?
        
        self._success()
    def onNoFix(self, gps):
        self._failure()
        
        return False
    def onConnected(self, connection):
        self.logger.debug("Device is now connected")
        self._success()
    
    # Update method
    def updateFirst(self):
        self.update()
        return False
    def update(self):
        self.logger.info("Updating the location")
        
        global connection
        self.state = self.State.CONNECTING
        if not connection.connected:
            self.logger.info("Connecting")
            connection.request()
            self.timeout = gobject.timeout_add(TIMEOUT_CONN * 1000, self._timeout)
        else:
            self._success()
        
        return True
    
    # Auxiliary
    def pushCache(self):
        # If the GPS is still running, don't push the latest entry (can still get updated)
        global gps
        keep_entries = 0
        if (gps.running):
            keep_entries = 1
        
        if (len(self.cache) > keep_entries):
            self.logger.info("Uploading entries")
            global service
            service.upload(self.cache[keep_entries:])
            del self.cache[keep_entries:]
        
        return False
    
    # State machine
    def _failure(self):
        global gps, connection
        
        if self.state == self.State.CONNECTING:
            self.logger.info("Failed to connect")
            self.state = self.State.IDLE
        elif self.state == self.State.UPDATING_WIFI:
            self.logger.info("WIFI lookup failed")
            gps.stop()
            
            self.logger.info("Attempting GSM lookup")
            self.state = self.State.UPDATING_GSM
            gps.start(GPSWrapper.Source.GSM, GPSWrapper.Aid.INTERNET)
            self.timeout = gobject.timeout_add(TIMEOUT_GSM * 1000, self._timeout)
        elif self.state == self.State.UPDATING_GSM:
            self.logger.info("GSM lookup failed")
            gps.stop()
            
            self.logger.info("Attempting GPS lookup")
            self.state = self.State.UPDATING_GPS
            gps.start(GPSWrapper.Source.GPS, GPSWrapper.Aid.INTERNET)
            self.timeout = gobject.timeout_add(TIMEOUT_GPS * 1000, self._timeout)
        elif self.state == self.State.UPDATING_GPS:
            self.logger.info("GPS lookup failed")
            gps.stop()
            self.state = self.State.IDLE
    def _success(self):
        global gps, connection
        
        if self.state == self.State.CONNECTING:
            self.logger.info("Successfully connected")
            if self.timeout != None:
                gobject.source_remove(self.timeout)
            self.timeout = None
            
            self.logger.info("Attempting WIFI lookup")
            self.state = self.State.UPDATING_WIFI
            gps.start(GPSWrapper.Source.WIFI, GPSWrapper.Aid.INTERNET)
        elif self.state == self.State.UPDATING_WIFI:
            self.logger.info("WIFI lookup succeeded")
            gps.stop()
            
            self.pushCache()
            
            self.state = self.State.IDLE
        elif self.state == self.State.UPDATING_GSM:
            self.logger.info("GSM lookup succeeded")
            gps.stop()
            if self.timeout != None:
                gobject.source_remove(self.timeout)
            self.timeout = None
            
            self.pushCache()
            
            self.state = self.State.IDLE
        elif self.state == self.State.UPDATING_GPS:
            self.logger.info("GPS lookup succeeded")
            gps.stop()
            if self.timeout != None:
                gobject.source_remove(self.timeout)
            self.timeout = None
            
            self.pushCache()
            
            self.state = self.State.IDLE
    def _timeout(self):
        self.logger.info("Timeout hit")
        self._failure()

#
# Application handling
#

def init():    
    # Configure the device wrapper
    global device
    device = DeviceWrapper()
    
    # Configure the GPS wrapper
    global gps
    gps = GPSWrapper()
    
    # Configure the connection wrapper
    global connection
    connection = ConnectionWrapper()
    
    # Configure the service wrapper
    global service
    service = ServiceWrapper()
    
    # Install the actor
    global actor
    actor = Actor()
    
    # Schedule updates
    gobject.timeout_add(UPDATE_AT_LEAST * 60000, actor.update)
    gobject.idle_add(actor.updateFirst)

def daemonize():
    pid = os.fork()
    if (pid == 0):
        os.setsid()
        pid = os.fork()
        if (pid == 0):
            os.umask(0)
        else:
            os._exit(0)
    else:
        os._exit(0)

    os.close(0);
    os.close(1);
    os.close(2);

def main(args):
    # Configure logging
    rootlogger = logging.getLogger()
    filehandler = logging.FileHandler('/var/log/efficient-latitude.log')
    filehandler.setFormatter(logging.Formatter('%(name)s:%(levelname)s %(module)s:%(lineno)d:  %(message)s'))
    rootlogger.addHandler(filehandler)
    streamhandler = logging.StreamHandler()
    streamhandler.setFormatter(logging.Formatter('%(levelname)-10s %(message)s'))
    rootlogger.addHandler(streamhandler)
    
    # Process command-line arguments
    if args.verbose:
        rootlogger.setLevel(logging.DEBUG)
    else:
        rootlogger.setLevel(logging.INFO)
    
    rootlogger.info('Initializing application')
    init()
    
    if args.daemonize:
        rootlogger.info('Forking into the background')
        daemonize()
    gobject.MainLoop().run()


parser = argparse.ArgumentParser(description='Intelligent Google Latitude updater.')
parser.add_argument('--verbose', '-v', help='print more information', action='store_true')
parser.add_argument('--daemonize', '-d', help='fork in the background', action='store_true')
main(parser.parse_args())

