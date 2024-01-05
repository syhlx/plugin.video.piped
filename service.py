from http.server import SimpleHTTPRequestHandler
import socketserver
import requests
import json
from urllib.parse import parse_qsl
import threading

import xbmc
import xbmcaddon
import xbmcvfs

from lib.utils import get_component
from default import mark_as_watched

addon = xbmcaddon.Addon()
profile_path: str = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("profile"))

instance: str = addon.getSettingString('instance')

def generate_dash(video_id: str) -> str:
	resp = requests.get(f'{instance}/streams/{video_id}').json()

	streams: dict = dict(
		audio = dict(),
		video = dict(),
	)

	default_audio: dict = dict()
	prefer_original_lang: bool = addon.getSettingBool("audio_prefer_original_lang")
	preferred_lang: str = addon.getSettingString("audio_custom_lang") if addon.getSettingString("audio_custom_lang") and not addon.getSettingBool("audio_prefer_kodi_lang") else xbmc.getLanguage(xbmc.ISO_639_1)

	for stream in resp["audioStreams"] + resp["videoStreams"]:
		media_lang = stream["audioTrackLocale"] if stream["audioTrackLocale"] is not None else 'null'
		media_type, media_format = stream["mimeType"].split("/")
		if media_type in ['audio', 'video'] and "googlevideo" in stream["url"]:
			if media_type == 'audio' and ((prefer_original_lang and stream["audioTrackType"] == "ORIGINAL") or (not prefer_original_lang and media_lang[:2] == preferred_lang)):
				if not default_audio.__contains__(media_lang): default_audio[media_lang]: dict = dict()
				if not default_audio[media_lang].__contains__(media_format): default_audio[media_lang][media_format]: list = list()
				default_audio[media_lang][media_format].append(stream)
			else:
				if not streams[media_type].__contains__(media_lang): streams[media_type][media_lang]: dict = dict()
				if not streams[media_type][media_lang].__contains__(media_format): streams[media_type][media_lang][media_format]: list = list()
				streams[media_type][media_lang][media_format].append(stream)

	streams['audio'] = default_audio | streams['audio']

	mpd: str = '<?xml version="1.0" encoding="utf-8"?>'
	mpd += f'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" profiles="urn:mpeg:dash:profile:full:2011" minBufferTime="PT1.5S" type="static" mediaPresentationDuration="PT{resp["duration"]}S">'

	mpd += '<Period>'

	if addon.getSettingBool("subtitles_load"):
		for subtitle in resp["subtitles"]:
			mpd += f'<AdaptationSet contentType="text" mimeType="{subtitle["mimeType"]}" segmentAlignment="true" lang="{subtitle["code"]}">'
			mpd += '<Role schemeIdUri="urn:mpeg:dash:role:2011" value="subtitle"/>'
			mpd += f'<Representation id="caption_{subtitle["code"]}{"_auto" if subtitle["autoGenerated"] else ""}" bandwidth="256">'
			mpd += f'<BaseURL>{subtitle["url"].replace("&", "&amp;")}</BaseURL>'
			mpd += '</Representation></AdaptationSet>'

	for stream_type in ['audio', 'video']:
		for stream_lang in streams[stream_type]:
			for stream_format in streams[stream_type][stream_lang]:
				stream_xml: str = ''
				for stream in streams[stream_type][stream_lang][stream_format]:
					if stream["initEnd"] > 0:
						stream_xml += f'<Representation id="{stream["itag"]}" codecs="{stream["codec"]}" bandwidth="{stream["bitrate"]}"'
						if stream_type == 'audio':
							stream_xml += '><AudioChannelConfiguration schemeIdUri="urn:mpeg:dash:23003:3:audio_channel_configuration:2011" value="2"/>'
						elif stream_type == 'video':
							stream_xml += f' width="{stream["width"]}" height="{stream["height"]}" maxPlayoutRate="1" frameRate="{stream["fps"]}">'
						stream_xml += f'<BaseURL>{stream["url"].replace("&", "&amp;")}</BaseURL>'
						stream_xml += f'<SegmentBase indexRange="{stream["indexStart"]}-{stream["indexEnd"]}">'
						stream_xml += f'<Initialization range="{stream["initStart"]}-{stream["initEnd"]}"/>'
						stream_xml += '</SegmentBase></Representation>'

				if len(stream_xml) > 0:
					mpd += f'<AdaptationSet mimeType="{stream_type}/{stream_format}" startWithSAP="1" subsegmentAlignment="true"'
					mpd += ' scanType="progressive">' if stream_type == 'video' else f' lang="{stream_lang}">'
					mpd += stream_xml
					mpd += f'</AdaptationSet>'

	mpd += '</Period></MPD>'

	return mpd


class HttpRequestHandler(SimpleHTTPRequestHandler):
	def do_GET(self):
		if self.path.startswith('/watch?v='):
			self.send_response(200)
			self.send_header('Content type', 'application/dash+xml')
			self.end_headers()
			self.wfile.write(generate_dash(parse_qsl(self.path)[0][1]).encode('utf-8'))
		else:
			self.send_error(404, "File not Found")

	def do_HEAD(self):
		if self.path.startswith('/watch?v='):
			self.send_response(200)
			self.send_header('Content type', 'application/dash+xml')
			self.end_headers()
		else:
			self.send_error(404, "File not Found")

		return

def HttpService():
	http_port: int = 0 if addon.getSettingBool('http_port_random') else addon.getSettingInt('http_port')
	with socketserver.TCPServer(('127.0.0.1', http_port), HttpRequestHandler) as httpd:
		addon.setSettingInt('http_port', httpd.socket.getsockname()[1])
		httpd.serve_forever()

class Scheduler(threading.Thread):
	def __init__(self):
		super(Scheduler, self).__init__()
		self._stop_event = threading.Event()

	def run(self):
		while not self._stop_event.is_set():
			if addon.getSettingBool('watch_history_enable') and len(addon.getSettingString('watch_history_playlist')) > 0:
				history: list = list()
				history_playlist: list = requests.get(f"{instance}/playlists/{addon.getSettingString('watch_history_playlist')}").json()['relatedStreams']
		
				for watched in history_playlist:
					history.append(get_component(watched['url'])['params']['v'])
		
				history.reverse()
		
				if len(history) > 0:
					with open(f'{profile_path}/watch_history.json', 'w+') as f:
						json.dump(history, f)
		
			self._stop_event.wait(addon.getSettingInt('watch_history_refresh') * 60_000)

	def stop(self, timeout=1):
		self._stop_event.set()
		self.join(timeout)

class Player(xbmc.Player):
	def __init__(self):
		super(xbmc.Player, self).__init__()

		self.video_id = None

	def watchHistoryEnabled(self):
		return addon.getSettingBool('watch_history_enable') and len(addon.getSettingString('watch_history_playlist')) > 0

	def markAsWatched(self):
		if self.watchHistoryEnabled() and self.video_id is not None:
			mark_as_watched(self.video_id)
			self.video_id = None

	def onPlayBackStarted(self):
		video_id = self.getPlayingItem().getProperty('piped_video_id')
		self.video_id = video_id if isinstance(video_id, str) and len(video_id) > 5 else None

		while self.video_id is not None:
			try:
				time_remaining: float = self.getTotalTime() - self.getTime()
				if time_remaining > 0. and time_remaining < 5.: self.markAsWatched()
			except:
				pass

			xbmc.sleep(500)

	def onPlayBackEnded(self):
		self.markAsWatched()

	def onPlayBackStopped(self):
		self.video_id = None

	def onPlayBackSeek(self, time, seekOffset):
		try:
			if time + seekOffset > (self.getTotalTime() - 5) * 1000:
				self.markAsWatched()
		except:
			pass

class Service(xbmc.Monitor):
	def __init__(self):
		httpservice = threading.Thread(target=HttpService)
		scheduler = Scheduler()
		httpservice.daemon = True
		scheduler.daemon = True
		player = Player()
		player.isPlaying()

		while not self.abortRequested():
			if self.waitForAbort(5):
				while scheduler.is_alive():
					scheduler.stop()
					xbmc.sleep(200)
				break

			try:
				if not httpservice.is_alive(): httpservice.start()
				if not scheduler.is_alive(): scheduler.start()
			except:
				pass

if __name__ == '__main__':
	Service()
