from __future__ import print_function

# Plugins Config
from xml.etree.cElementTree import parse as cet_parse
from os import path as os_path
from AutoTimerConfiguration import parseConfig, buildConfig
from Tools.IO import saveFile

# Navigation (RecordTimer)
import NavigationInstance

# Timer
from ServiceReference import ServiceReference
from RecordTimer import RecordTimerEntry
from Components.TimerSanityCheck import TimerSanityCheck

# Timespan
from time import localtime, strftime, time, mktime
from datetime import timedelta, date

# EPGCache & Event
from enigma import eEPGCache, eServiceReference, eServiceCenter, iServiceInformation

from twisted.internet import reactor, defer
from twisted.python import failure
from threading import currentThread
import Queue

# AutoTimer Component
from AutoTimerComponent import preferredAutoTimerComponent
from Logger import doLog, startLog, getLog, doDebug

from itertools import chain
from collections import defaultdict
from difflib import SequenceMatcher
from operator import itemgetter

from Plugins.SystemPlugins.Toolkit.SimpleThread import SimpleThread

try:
	from Plugins.Extensions.SeriesPlugin.plugin import getSeasonEpisode4 as sp_getSeasonEpisode
except ImportError as ie:
	sp_getSeasonEpisode = None

try:
	from Plugins.Extensions.SeriesPlugin.plugin import showResult as sp_showResult
except ImportError as ie:
	sp_showResult = None

from . import config, xrange, itervalues

try:
	from Components.ServiceRecordingSettings import ServiceRecordingSettings
except ImportError as ie:
	ServiceRecordingSettings = None

XML_CONFIG = "/etc/enigma2/autotimer.xml"

TAG = "AutoTimer"

def getTimeDiff(timerbegin, timerend, begin, end):
	if begin <= timerbegin <= end:
		return end - timerbegin
	elif timerbegin <= begin <= timerend:
		return timerend - begin
	return 0

def blockingCallFromMainThread(f, *a, **kw):
	"""
	  Modified version of twisted.internet.threads.blockingCallFromThread
	  which waits 30s for results and otherwise assumes the system to be shut down.
	  This is an ugly workaround for a twisted-internal deadlock.
	  Please keep the look intact in case someone comes up with a way
	  to reliably detect from the outside if twisted is currently shutting
	  down.
	"""
	queue = Queue.Queue()
	def _callFromThread():
		result = defer.maybeDeferred(f, *a, **kw)
		result.addBoth(queue.put)
	reactor.callFromThread(_callFromThread)

	result = None
	while True:
		try:
			result = queue.get(True, config.plugins.autotimer.timeout.value*60)
		except Queue.Empty as qe:
			if True: #not reactor.running: # reactor.running is only False AFTER shutdown, we are during.
				doLog("Reactor no longer active, aborting.")
		else:
			break

	if isinstance(result, failure.Failure):
		result.raiseException()
	return result

typeMap = {
	"exact": eEPGCache.EXAKT_TITLE_SEARCH,
	"partial": eEPGCache.PARTIAL_TITLE_SEARCH,
	"description": eEPGCache.PARTIAL_DESCRIPTION_SEARCH
}

caseMap = {
	"sensitive": eEPGCache.CASE_CHECK,
	"insensitive": eEPGCache.NO_CASE_CHECK
}

class AutoTimer:
	"""Read and save xml configuration, query EPGCache"""

	def __init__(self):
		# Initialize
		self.timers = []
		self.configMtime = -1
		self.uniqueTimerId = 0
		self.defaultTimer = preferredAutoTimerComponent(
			0,		# Id
			"",		# Name
			"",		# Match
			True 	# Enabled
		)

# Configuration

	def readXml(self):
		# Abort if no config found
		if not os_path.exists(XML_CONFIG):
			doLog("No configuration file present")
			return

		# Parse if mtime differs from whats saved
		mtime = os_path.getmtime(XML_CONFIG)
		if mtime == self.configMtime:
			doLog("No changes in configuration, won't parse")
			return

		# Save current mtime
		self.configMtime = mtime

		# Parse Config
		configuration = cet_parse(XML_CONFIG).getroot()

		# Empty out timers and reset Ids
		del self.timers[:]
		self.defaultTimer.clear(-1, True)

		parseConfig(
			configuration,
			self.timers,
			configuration.get("version"),
			0,
			self.defaultTimer
		)
		self.uniqueTimerId = len(self.timers)

	def getXml(self):
		return buildConfig(self.defaultTimer, self.timers, webif = True)

	def writeXml(self):
		# XXX: we probably want to indicate failures in some way :)
		saveFile(XML_CONFIG, buildConfig(self.defaultTimer, self.timers))

# Manage List

	def add(self, timer):
		self.timers.append(timer)

	def getEnabledTimerList(self):
		return (x for x in self.timers if x.enabled)

	def getTimerList(self):
		return self.timers

	def getTupleTimerList(self):
		lst = self.timers
		return [(x,) for x in lst]

	def getSortedTupleTimerList(self):
		lst = self.timers[:]
		lst.sort()
		return [(x,) for x in lst]

	def getUniqueId(self):
		self.uniqueTimerId += 1
		return self.uniqueTimerId

	def remove(self, uniqueId):
		idx = 0
		for timer in self.timers:
			if timer.id == uniqueId:
				self.timers.pop(idx)
				return
			idx += 1

	def set(self, timer):
		idx = 0
		for stimer in self.timers:
			if stimer == timer:
				self.timers[idx] = timer
				return
			idx += 1
		self.timers.append(timer)

	def parseEPGAsync(self, simulateOnly = False):
		t = SimpleThread(lambda: self.parseEPG(simulateOnly=simulateOnly))
		t.start()
		return t.deferred

# Main function

	def parseTimer(self, timer, epgcache, serviceHandler, recordHandler, checkEvtLimit, evtLimit, timers, conflicting, similars, skipped, timerdict, moviedict, simulateOnly=False):
		new = 0
		modified = 0

		# Search EPG, default to empty list
		epgmatches = epgcache.search( ('RITBDSE', 1000, typeMap[timer.searchType], timer.match, caseMap[timer.searchCase]) ) or []

		# Sort list of tuples by begin time 'B'
		epgmatches.sort(key=itemgetter(3))

		# Contains the the marked similar eits and the conflicting strings
		similardict = defaultdict(list)		

		# Loop over all EPG matches
		for idx, ( serviceref, eit, name, begin, duration, shortdesc, extdesc ) in enumerate( epgmatches ):
			
			startLog()
			
			# timer destination dir
			dest = timer.destination or config.usage.default_path.value
			
			evtBegin = begin
			evtEnd = end = begin + duration

			doLog("possible epgmatch %s" % (name))
			doLog("Serviceref %s" % (str(serviceref)))
			eserviceref = eServiceReference(serviceref)
			evt = epgcache.lookupEventId(eserviceref, eit)
			if not evt:
				doLog("Could not create Event!")
				skipped.append((name, begin, end, str(serviceref), timer.name, getLog()))
				continue
			# Try to determine real service (we always choose the last one)
			n = evt.getNumOfLinkageServices()
			if n > 0:
				i = evt.getLinkageService(eserviceref, n-1)
				serviceref = i.toString()
				doLog("Serviceref2 %s" % (str(serviceref)))

			# If event starts in less than 60 seconds skip it
			if begin < time() + 60:
				doLog("Skipping an event because it starts in less than 60 seconds")
				skipped.append((name, begin, end, serviceref, timer.name, getLog()))
				continue

			# Convert begin time
			timestamp = localtime(begin)
			# Update timer
			timer.update(begin, timestamp)

			# Check if eit is in similar matches list
			# NOTE: ignore evtLimit for similar timers as I feel this makes the feature unintuitive
			similarTimer = False
			if eit in similardict:
				similarTimer = True
				dayofweek = None # NOTE: ignore day on similar timer
			else:
				# If maximum days in future is set then check time
				if checkEvtLimit:
					if begin > evtLimit:
						doLog("Skipping an event because of maximum days in future is reached")
						skipped.append((name, begin, end, serviceref, timer.name, getLog()))
						continue

				dayofweek = str(timestamp.tm_wday)

			# Check timer conditions
			# NOTE: similar matches do not care about the day/time they are on, so ignore them
			if timer.checkServices(serviceref):
				doLog("Skipping an event because of check services")
				skipped.append((name, begin, end, serviceref, timer.name, getLog()))
				continue
			if timer.checkDuration(duration):
				doLog("Skipping an event because of duration check")
				skipped.append((name, begin, end, serviceref, timer.name, getLog()))
				continue
			if not similarTimer:
				if timer.checkTimespan(timestamp):
					doLog("Skipping an event because of timestamp check")
					skipped.append((name, begin, end, serviceref, timer.name, getLog()))
					continue
				if timer.checkTimeframe(begin):
					doLog("Skipping an event because of timeframe check")
					skipped.append((name, begin, end, serviceref, timer.name, getLog()))
					continue

			# Initialize
			newEntry = None
			oldExists = False
			allow_modify = True
			
			# Eventually change service to alternative
			if timer.overrideAlternatives:
				serviceref = timer.getAlternative(serviceref)

			if timer.series_labeling and sp_getSeasonEpisode is not None:
				allow_modify = False
				#doLog("Request name, desc, path %s %s %s" % (name,shortdesc,dest))
				sp = sp_getSeasonEpisode(serviceref, name, evtBegin, evtEnd, shortdesc, dest)
				if sp and type(sp) in (tuple, list) and len(sp) == 4:
					name = sp[0] or name
					shortdesc = sp[1] or shortdesc
					dest = sp[2] or dest
					doLog(str(sp[3]))
					#doLog("Returned name, desc, path %s %s %s" % (name,shortdesc,dest))
					allow_modify = True
				else:
					# Nothing found
					doLog(str(sp))
					
					# If AutoTimer name not equal match, do a second lookup with the name
					if timer.name.lower() != timer.match.lower():
						#doLog("Request name, desc, path %s %s %s" % (timer.name,shortdesc,dest))
						sp = sp_getSeasonEpisode(serviceref, timer.name, evtBegin, evtEnd, shortdesc, dest)
						if sp and type(sp) in (tuple, list) and len(sp) == 4:
							name = sp[0] or name
							shortdesc = sp[1] or shortdesc
							dest = sp[2] or dest
							doLog(str(sp[3]))
							#doLog("Returned name, desc, path %s %s %s" % (name,shortdesc,dest))
							allow_modify = True
						else:
							doLog(str(sp))

			if timer.checkFilter(name, shortdesc, extdesc, dayofweek):
				doLog("Skipping an event because of filter check")
				skipped.append((name, begin, end, serviceref, timer.name, getLog()))
				continue
			
			if timer.hasOffset():
				# Apply custom Offset
				begin, end = timer.applyOffset(begin, end)
			else:
				# Apply E2 Offset
				if ServiceRecordingSettings:
					begin -= ServiceRecordingSettings.instance.getMarginBefore(eserviceref)
					end += ServiceRecordingSettings.instance.getMarginAfter(eserviceref)
				else:
					begin -= config.recording.margin_before.value * 60
					end += config.recording.margin_after.value * 60

			# Overwrite endtime if requested
			if timer.justplay and not timer.setEndtime:
				end = begin

			# Check for existing recordings in directory
			if timer.avoidDuplicateDescription == 3:
				# Reset movie Exists
				movieExists = False

				if dest and dest not in moviedict:
					self.addDirectoryToMovieDict(moviedict, dest, serviceHandler)
				for movieinfo in moviedict.get(dest, ()):
					if self.checkDuplicates(timer, name, movieinfo.get("name"), shortdesc, movieinfo.get("shortdesc"), extdesc, movieinfo.get("extdesc") ):
						doLog("We found a matching recorded movie, skipping event:", name)
						movieExists = True
						break
				if movieExists:
					doLog("Skipping an event because movie already exists")
					skipped.append((name, begin, end, serviceref, timer.name, getLog()))
					continue

			# Check for double Timers
			# We first check eit and if user wants us to guess event based on time
			# we try this as backup. The allowed diff should be configurable though.
			for rtimer in timerdict.get(serviceref, ()):
				if rtimer.eit == eit:
					oldExists = True
					doLog("We found a timer based on eit")
					newEntry = rtimer
					break
				elif config.plugins.autotimer.try_guessing.value:
					if timer.hasOffset():
						# Remove custom Offset
						rbegin = rtimer.begin + timer.offset[0] * 60
						rend = rtimer.end - timer.offset[1] * 60
					else:
						# Remove E2 Offset
						rbegin = rtimer.begin + config.recording.margin_before.value * 60
						rend = rtimer.end - config.recording.margin_after.value * 60
					# As alternative we could also do a epg lookup
					#revent = epgcache.lookupEventId(rtimer.service_ref.ref, rtimer.eit)
					#rbegin = revent.getBeginTime() or 0
					#rduration = revent.getDuration() or 0
					#rend = rbegin + rduration or 0
					if getTimeDiff(rbegin, rend, evtBegin, evtEnd) > ((duration/10)*8):
						oldExists = True
						doLog("We found a timer based on time guessing")
						newEntry = rtimer
						break
				if timer.avoidDuplicateDescription >= 1 \
					and not rtimer.disabled:
						if self.checkDuplicates(timer, name, rtimer.name, shortdesc, rtimer.description, extdesc, rtimer.extdesc ):
						# if searchForDuplicateDescription > 1 then check short description
							oldExists = True
							doLog("We found a timer (similar service) with same description, skipping event")
							break

			# We found no timer we want to edit
			if newEntry is None:
				# But there is a match
				if oldExists:
					doLog("Skipping an event because a timer on same service exists")
					skipped.append((name, begin, end, serviceref, timer.name, getLog()))
					continue

				# We want to search for possible doubles
				if timer.avoidDuplicateDescription >= 2:
					for rtimer in chain.from_iterable( itervalues(timerdict) ):
						if not rtimer.disabled:
							if self.checkDuplicates(timer, name, rtimer.name, shortdesc, rtimer.description, extdesc, rtimer.extdesc ):
								oldExists = True
								doLog("We found a timer (any service) with same description, skipping event")
								break
					if oldExists:
						doLog("Skipping an event because a timer on any service exists")
						skipped.append((name, begin, end, serviceref, timer.name, getLog()))
						continue

				if timer.checkCounter(timestamp):
					doLog("Not adding new timer because counter is depleted.")
					skipped.append((name, begin, end, serviceref, timer.name, getLog()))
					continue

			# Append to timerlist and abort if simulating
			timers.append((name, begin, end, serviceref, timer.name, getLog()))
			if simulateOnly:
				continue

			if newEntry is not None:
				# Abort if we don't want to modify timers or timer is repeated
				if config.plugins.autotimer.refresh.value == "none" or newEntry.repeated:
					doLog("Won't modify existing timer because either no modification allowed or repeated timer")
					continue

				if hasattr(newEntry, "isAutoTimer"):
					msg = "[AutoTimer] AutoTimer %s modified this automatically generated timer." % (timer.name)
					doLog(msg)
					newEntry.log(501, msg)
				elif config.plugins.autotimer.add_autotimer_to_tags.value and TAG in newEntry.tags:
					msg = "[AutoTimer] AutoTimer %s modified this automatically generated timer." % (timer.name)
					doLog(msg)
					newEntry.log(501, msg)
				else:
					if config.plugins.autotimer.refresh.value != "all":
						doLog("Won't modify existing timer because it's no timer set by us")
						continue

					msg = "[AutoTimer] Warning, AutoTimer %s messed with a timer which might not belong to it: %s ." % (timer.name, newEntry.name)
					doLog(msg)
					newEntry.log(501, msg)

				modified += 1

				if allow_modify:
					self.modifyTimer(newEntry, name, shortdesc, begin, end, serviceref, eit)
					msg = "[AutoTimer] AutoTimer modified timer: %s ." % (newEntry.name)
					doLog(msg)
					newEntry.log(501, msg)
				else:
					msg = "[AutoTimer] AutoTimer modification not allowed for timer: %s ." % (newEntry.name)
					doLog(msg)
			else:
				newEntry = RecordTimerEntry(ServiceReference(serviceref), begin, end, name, shortdesc, eit)
				msg = "[AutoTimer] Try to add new timer based on AutoTimer %s." % (timer.name)
				doLog(msg)
				newEntry.log(500, msg)
				
				# Mark this entry as AutoTimer (only AutoTimers will have this Attribute set)
				# It is only temporarily, after a restart it will be lost,
				# because it won't be stored in the timer xml file
				newEntry.isAutoTimer = True

			# Apply afterEvent
			if timer.hasAfterEvent():
				afterEvent = timer.getAfterEventTimespan(localtime(end))
				if afterEvent is None:
					afterEvent = timer.getAfterEvent()
				if afterEvent is not None:
					newEntry.afterEvent = afterEvent

			newEntry.dirname = dest
			newEntry.calculateFilename()

			newEntry.justplay = timer.justplay
			newEntry.vpsplugin_enabled = timer.vps_enabled
			newEntry.vpsplugin_overwrite = timer.vps_overwrite
			tags = timer.tags[:]
			if config.plugins.autotimer.add_autotimer_to_tags.value:
				if TAG not in tags:
					tags.append(TAG)
			if config.plugins.autotimer.add_name_to_tags.value:
				tagname = timer.name.strip()
				if tagname:
					tagname = tagname[0].upper() + tagname[1:].replace(" ", "_")
					if tagname not in tags:
						tags.append(tagname)
			newEntry.tags = tags

			if oldExists:
				# XXX: this won't perform a sanity check, but do we actually want to do so?
				recordHandler.timeChanged(newEntry)

			else:
				conflictString = ""
				if similarTimer:
					conflictString = similardict[eit].conflictString
					msg = "[AutoTimer] Try to add similar Timer because of conflicts with %s." % (conflictString)
					doLog(msg)
					newEntry.log(504, msg)

				# Try to add timer
				conflicts = recordHandler.record(newEntry)

				if conflicts:
					# Maybe use newEntry.log
					conflictString += ' / '.join(["%s (%s)" % (x.name, strftime("%Y%m%d %H%M", localtime(x.begin))) for x in conflicts])
					doLog("conflict with %s detected" % (conflictString))

					if config.plugins.autotimer.addsimilar_on_conflict.value:
						# We start our search right after our actual index
						# Attention we have to use a copy of the list, because we have to append the previous older matches
						lepgm = len(epgmatches)
						for i in xrange(lepgm):
							servicerefS, eitS, nameS, beginS, durationS, shortdescS, extdescS = epgmatches[ (i+idx+1)%lepgm ]
							if self.checkDuplicates(timer, name, nameS, shortdesc, shortdescS, extdesc, extdescS, force=True ):
								# Check if the similar is already known
								if eitS not in similardict:
									doLog("Found similar Timer: " + name)

									# Store the actual and similar eit and conflictString, so it can be handled later
									newEntry.conflictString = conflictString
									similardict[eit] = newEntry
									similardict[eitS] = newEntry
									similarTimer = True
									if beginS <= evtBegin:
										# Event is before our actual epgmatch so we have to append it to the epgmatches list
										epgmatches.append((servicerefS, eitS, nameS, beginS, durationS, shortdescS, extdescS))
									# If we need a second similar it will be found the next time
								else:
									similarTimer = False
									newEntry = similardict[eitS]
								break

				if conflicts is None:
					timer.decrementCounter()
					new += 1
					newEntry.extdesc = extdesc
					timerdict[serviceref].append(newEntry)

					# Similar timers are in new timers list and additionally in similar timers list
					if similarTimer:
						similars.append((name, begin, end, serviceref, timer.name))
						similardict.clear()

				# Don't care about similar timers
				elif not similarTimer:
					conflicting.append((name, begin, end, serviceref, timer.name))

					if config.plugins.autotimer.disabled_on_conflict.value:
						msg = "[AutoTimer] Timer disabled because of conflicts with %s." % (conflictString)
						doLog(msg)
						newEntry.log(503, msg)
						newEntry.disabled = True
						# We might want to do the sanity check locally so we don't run it twice - but I consider this workaround a hack anyway
						conflicts = recordHandler.record(newEntry)
		
		return (new, modified)

	def parseEPG(self, simulateOnly=False, uniqueId=None, callback=None):

		from plugin import AUTOTIMER_VERSION
		doLog("AutoTimer Version: " + AUTOTIMER_VERSION)

		if NavigationInstance.instance is None:
			doLog("Navigation is not available, can't parse EPG")
			return (0, 0, 0, [], [], [])

		new = 0
		modified = 0
		timers = []
		conflicting = []
		similars = []
		skipped = []

		if currentThread().getName() == 'MainThread':
			doBlockingCallFromMainThread = lambda f, *a, **kw: f(*a, **kw)
		else:
			doBlockingCallFromMainThread = blockingCallFromMainThread

		# NOTE: the config option specifies "the next X days" which means today (== 1) + X
		delta = timedelta(days = config.plugins.autotimer.maxdaysinfuture.value + 1)
		evtLimit = mktime((date.today() + delta).timetuple())
		checkEvtLimit = delta.days > 1
		del delta

		# Read AutoTimer configuration
		self.readXml()

		# Get E2 instances
		epgcache = eEPGCache.getInstance()
		serviceHandler = eServiceCenter.getInstance()
		recordHandler = NavigationInstance.instance.RecordTimer

		# Save Timer in a dict to speed things up a little
		# We include processed timers as we might search for duplicate descriptions
		# NOTE: It is also possible to use RecordTimer isInTimer(), but we won't get the timer itself on a match
		timerdict = defaultdict(list)
		doBlockingCallFromMainThread(self.populateTimerdict, epgcache, recordHandler, timerdict)

		# Create dict of all movies in all folders used by an autotimer to compare with recordings
		# The moviedict will be filled only if one AutoTimer is configured to avoid duplicate description for any recordings
		moviedict = defaultdict(list)

		# Iterate Timer
		for timer in self.getEnabledTimerList():
			if uniqueId == None or timer.id == uniqueId:
				tup = doBlockingCallFromMainThread(self.parseTimer, timer, epgcache, serviceHandler, recordHandler, checkEvtLimit, evtLimit, timers, conflicting, similars, skipped, timerdict, moviedict, simulateOnly=simulateOnly)
				if callback:
					callback(timers, conflicting, similars, skipped)
					del timers[:]
					del conflicting[:]
					del similars[:]
					del skipped[:]
				else:
					new += tup[0]
					modified += tup[1]
		
		if not simulateOnly:
			if sp_showResult is not None:
				blockingCallFromMainThread(sp_showResult)
		
		return (len(timers), new, modified, timers, conflicting, similars)

# Supporting functions

	def populateTimerdict(self, epgcache, recordHandler, timerdict):
		remove = []
		for timer in chain(recordHandler.timer_list, recordHandler.processed_timers):
			if timer and timer.service_ref:
				if timer.eit is not None:
					event = epgcache.lookupEventId(timer.service_ref.ref, timer.eit)
					if event:
						timer.extdesc = event.getExtendedDescription()
					else:
						remove.append(timer)
				else:
					remove.append(timer)
					continue

				if not hasattr(timer, 'extdesc'):
					timer.extdesc = ''

				timerdict[str(timer.service_ref)].append(timer)

		if config.plugins.autotimer.check_eit_and_remove.value:
			for timer in remove:
				if hasattr(timer, "isAutoTimer") or (config.plugins.autotimer.add_autotimer_to_tags.value and TAG in timer.tags):
					try:
						# Because of the duplicate check, we only want to remove future timer
						if timer in recordHandler.timer_list:
							if not timer.isRunning():
								global NavigationInstance
								doLog("Remove timer because of eit check " + timer.name)
								NavigationInstance.instance.RecordTimer.removeEntry(timer)
					except:
						pass
		del remove

	def modifyTimer(self, timer, name, shortdesc, begin, end, serviceref, eit=None):
		timer.name = name
		timer.description = shortdesc
		timer.begin = int(begin)
		timer.end = int(end)
		timer.service_ref = ServiceReference(serviceref)
		if eit:
			timer.eit = eit

	def addDirectoryToMovieDict(self, moviedict, dest, serviceHandler):
		movielist = serviceHandler.list(eServiceReference("2:0:1:0:0:0:0:0:0:0:" + dest))
		if movielist is None:
			doLog("listing of movies in " + dest + " failed")
		else:
			append = moviedict[dest].append
			while 1:
				movieref = movielist.getNext()
				if not movieref.valid():
					break
				if movieref.flags & eServiceReference.mustDescent:
					continue
				info = serviceHandler.info(movieref)
				if info is None:
					continue
				event = info.getEvent(movieref)
				if event is None:
					continue
				append({
					"name": info.getName(movieref),
					"shortdesc": info.getInfoString(movieref, iServiceInformation.sDescription),
					"extdesc": event.getExtendedDescription() or '' # XXX: does event.getExtendedDescription() actually return None on no description or an empty string?
				})

	def checkDuplicates(self, timer, name1, name2, shortdesc1, shortdesc2, extdesc1, extdesc2, force=False):
		if name1 and name2:
			sequenceMatcher = SequenceMatcher(" ".__eq__, name1, name2)
		else:
			return False

		ratio = sequenceMatcher.ratio()
		doDebug("names ratio %f - %s - %d - %s - %d" % (ratio, name1, len(name1), name2, len(name2)))
		if name1 in name2 or (0.9 < ratio): # this is probably a match
			foundShort = True
			if (force or timer.searchForDuplicateDescription > 0) and shortdesc1 and shortdesc2:
				sequenceMatcher.set_seqs(shortdesc1, shortdesc2)
				ratio = sequenceMatcher.ratio()
				doDebug("shortdesc ratio %f - %s - %d - %s - %d" % (ratio, shortdesc1, len(shortdesc1), shortdesc2, len(shortdesc2)))
				foundShort = shortdesc1 in shortdesc2 or (0.9 < ratio)
				if foundShort:
					doLog("shortdesc ratio %f - %s - %d - %s - %d" % (ratio, shortdesc1, len(shortdesc1), shortdesc2, len(shortdesc2)))

			foundExt = True
			# NOTE: only check extended if short description already is a match because otherwise
			# it won't evaluate to True anyway
			if foundShort and (force or timer.searchForDuplicateDescription > 1) and extdesc1 and extdesc2:
				sequenceMatcher.set_seqs(extdesc1, extdesc2)
				ratio = sequenceMatcher.ratio()
				doDebug("extdesc ratio %f - %s - %d - %s - %d" % (ratio, extdesc1, len(extdesc1), extdesc2, len(extdesc2)))
				foundExt = (0.9 < ratio)
				if foundExt:
					doLog("extdesc ratio %f - %s - %d - %s - %d" % (ratio, extdesc1, len(extdesc1), extdesc2, len(extdesc2)))
			return foundShort and foundExt
