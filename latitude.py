#!/usr/bin/python

################################################################################
# Configuration
#

# System modules
import argparse	# python-argparse
import gobject
import pickle
import time
import os
import sys
import logging
import signal

# Geolocation
import location	# python-location

# Network connectivity
import conic

# Google Latitude
import httplib2
import pickle
from apiclient.discovery import build
from apiclient.oauth import FlowThreeLegged
from apiclient.ext.authtools import run
from apiclient.ext.file import Storage


# Definitions
EPS			   = 0.001  # Points within this many degrees are considered "same" and not sent
UPDATE_AT_MOST	= 1	  # NEVER update more than this (minutes) even when moving
UPDATE_AT_LEAST   = 60	 # NEVER update LESS than this (minutes) even when still (to avoid "stale points" in Latitude)
GPS_INTERVAL	  = 5	  # How often to "awaken" the GPS (minutes)


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
	def upload(self,  entries):
		for entry in entries:
			self.service.location().insert(body = entry.getData()).execute()

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
	def on_connection_event(self,  connection, event):	
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

class GPSWrapper(gobject.GObject):
	# Signals
	__gsignals__ = {
		"fix": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE, (object, )),
		"start": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE, ()),
		"stop": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE, ()),
	}
	
	# Member data
	logger = logging.getLogger('GPSWrapper')
	control = location.GPSDControl.get_default()
	device = location.GPSDevice()
	fix_tries = 0
	running = False
	owned = False	
	acclimit=150
	location_current = Location()
	location_previous = Location()
	
	# Constructor
	def __init__(self):
		gobject.GObject.__init__(self)
		
		# Configure the device
		self.control.set_properties(preferred_method = location.METHOD_USER_SELECTED, preferred_interval=location.INTERVAL_120S)
		
		# Listen for events
		self.control.connect("gpsd-running", self.onStart)
		self.control.connect("gpsd-stopped", self.onStop)
		self.control.connect("error-verbose", self.onError)
		self.device.connect("changed", self.onChanged)
	
	# Events
	def onStart(self,  control):
		self.logger.debug("Control started")
		self.running = True
		self.emit("start")
	def onStop(self,  control):
		self.logger.debug("Control stopped")
		self.running = False
		self.emit("stop")
	def onError(self,  control, error):
		self.logger.error("GPS error: %d" % error)
	def onChanged(self,  device):
		if not device:
			return
		if device.fix and device.fix[1] & location.GPS_DEVICE_LATLONG_SET:
			self.process(device.fix[0], device.fix[4], device.fix[5], device.fix[6]/1000, device.fix[7], device.fix[8], device.fix[9], device.fix[11])
	
	# Location handling
	def process(self,  mode, lat, lng, acc, alt, altacc, head, speed):		
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
		
		# Ignore cached or country-size measurements
		if mode < 2:
			return
		self.logger.debug("Received raw location data mode %d (attempt %d): lat=%f, lon=%f (accuracy of %f) alt=%f (accuracy of %f), head=%f, speed=%f" % (mode, self.fix_tries, lat, lng, acc, alt, altacc, head, speed))

		# Skip the NaN's in accuracy
		if acc != acc:
			return

			# I don't care about data of low accuracy
		if acc > self.acclimit:
			return

		if altacc > 32000:
			altacc=0

		self.fix_tries += 1

		# Try at least three times to get a "type 3 fix"
		if mode < 3 and self.fix_tries < 3:
			return

		self.fix_tries = 0
		
		self.location_previous = self.location_current
		self.location_current = Location()
		self.location_current.time = time.time()
		self.location_current.lat = lat
		self.location_current.lng = lng
		self.location_current.acc = acc
		self.location_current.alt = alt
		self.location_current.altacc = altacc
		self.location_current.head = head
		self.location_current.speed = speed
		
		self.logger.debug("Emitting GPS fix")
		self.emit("fix",  self.location_current)
	
	# Actions
	def start(self):
		self.owned = True
		self.control.start()
	def stop(self):
		self.owned = False
		self.control.stop()

class PassiveActor(gobject.GObject):
	# Signals
	__gsignals__ = {
		"push": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE, ()),
	}
	
	# Member data
	logger = logging.getLogger('PassiveActor')
	cache = []
	
	# Constructor
	def __init__(self):
		gobject.GObject.__init__(self)
		
		# Listen for GPS events
		global gps
		gps.connect("fix",  self.onFix)
		gps.connect("stop", self.onStop)
		
		# Listen for connection events
		global connection
		connection.connect("connected",  self.onConnected)
	
	# Events
	def onFix(self,  gps,  location):
		self.logger.debug("Got a fix")
		
		if (location.lat == 0 or location.lng == 0):
			return
		
		# Fill the cache
		if (len(self.cache) == 0):
			self.cache.append(location)
		elif (time.time()  - self.cache[-1].time > UPDATE_AT_MOST * 60):
			cache.append(location)
		elif (self.cache[-1].acc > location.acc):
			self.cache[-1] = location
		else:
			return
		
		# Check if we are connected
		global connection
		if (connection.connected):
			self.pushCache()
		
		return
	def onStop(self,  gps):
		self.pushCache()
	def onConnected(self,  connection):
		self.logger.debug("Device is now connected")
		self.pushCache()
		return
	
	# Auxiliary
	def pushCache(self):
		# If the GPS is running, don't push the latest entry (can still get updated)
		keep_entries = 0
		if (gps.running):
			keep_entries = 1
		
		if (len(self.cache) > keep_entries):
			self.logger.info("Uploading entries")
			global service
			service.upload(self.cache[keep_entries:])
			del self.cache[keep_entries:]
			self.emit("push")
		
		return False

class ActiveActor:
	# Member data
	logger = logging.getLogger('ActiveActor')
	
	# Constructor
	def __init__(self):
		# Listen for passive actor events
		global actor_passive
		actor_passive.connect("push",  self.onPush)
		
		# Listen for GPS events
		global gps
		gps.connect("fix",  self.onFix)
	
	# Events
	def onPush(self,  actor_passive):
		self.logger.debug("Detected a push")
	def onFix(self,  gps,  location):
		if (gps.owned):
			self.logger.debug("Disabling GPS")
			gps.stop()
	
	# Update method
	def update(self):
		self.logger.info("Updating the location")
		
		self.logger.info("Enabling GPS")
		global gps
		gps.start()

#
# Actors
#

def actor_passive():
	print "boo"
	logging.info("Passively looking for a fix")


#
# Application handling
#

def init():	
	# Configure the GPS wrapper
	global gps
	gps = GPSWrapper()
	
	# Configure the connection wrapper
	global connection
	connection = ConnectionWrapper()
	
	# Configure the service wrapper
	global service
	service = ServiceWrapper()
	
	# Install the passive actor
	global actor_passive
	actor_passive = PassiveActor()
	
	# Schedule an initial active actor
	global actor_active
	actor_active = ActiveActor()
	gobject.timeout_add(GPS_INTERVAL * 60000, actor_active.update)
	gobject.idle_add(actor_active.update)

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
	# Process command-line arguments
	if args.verbose:
		logging.basicConfig(level=logging.DEBUG)
	else:
		logging.basicConfig(level=logging.INFO)
	
	logging.info('Initializing application')
	init()
	
	if args.daemonize:
		logging.info('Forking into the background')
		daemonize()
	gobject.MainLoop().run()


parser = argparse.ArgumentParser(description='Intelligent Google Latitude updater.')
parser.add_argument('--verbose', '-v', help='print more information', action='store_true')
parser.add_argument('--daemonize', '-d', help='fork in the background', action='store_true')
main(parser.parse_args())

