#!/usr/bin/env python2
# -*- coding: utf-8-*-
import os
import wave
import json
import tempfile
import logging
import urllib
from urllib import parse
import re
import subprocess
from abc import ABCMeta, abstractmethod
import requests
import yaml
from client import jasperpath
from client import diagnose
from client import vocabcompiler


class AbstractSTTEngine(object):
    """
    Generic parent class for all STT engines
    """

    __metaclass__ = ABCMeta
    VOCABULARY_TYPE = None

    @classmethod
    def get_config(cls):
        return {}

    @classmethod
    def get_instance(cls, vocabulary_name, phrases):
        config = cls.get_config()
        if cls.VOCABULARY_TYPE:
            vocabulary = cls.VOCABULARY_TYPE(vocabulary_name,
                                             path=jasperpath.config(
                                                 'vocabularies'))
            if not vocabulary.matches_phrases(phrases):
                vocabulary.compile(phrases)
            config['vocabulary'] = vocabulary
        instance = cls(**config)
        return instance

    @classmethod
    def get_passive_instance(cls):
        if cls.VOCABULARY_TYPE:
            phrases = vocabcompiler.get_keyword_phrases()
        else:
            phrases = None
        return cls.get_instance('keyword', phrases)

    @classmethod
    def get_active_instance(cls):
        if cls.VOCABULARY_TYPE:
            phrases = vocabcompiler.get_all_phrases()
        else:
            phrases = None
        return cls.get_instance('default', phrases)

    @classmethod
    @abstractmethod
    def is_available(cls):
        return True

    @abstractmethod
    def transcribe(self, fp):
        pass


class GoogleSTT(AbstractSTTEngine):
    """
    Speech-To-Text implementation which relies on the Google Speech API.

    This implementation requires a Google API key to be present in profile.yml

    To obtain an API key:
    1. Join the Chromium Dev group:
       https://groups.google.com/a/chromium.org/forum/?fromgroups#!forum/chromium-dev
    2. Create a project through the Google Developers console:
       https://console.developers.google.com/project
    3. Select your project. In the sidebar, navigate to "APIs & Auth." Activate
       the Speech API.
    4. Under "APIs & Auth," navigate to "Credentials." Create a new key for
       public API access.
    5. Add your credentials to your profile.yml. Add an entry to the 'keys'
       section using the key name 'GOOGLE_SPEECH.' Sample configuration:
    6. Set the value of the 'stt_engine' key in your profile.yml to 'google'


    Excerpt from sample profile.yml:

        ...
        timezone: US/Pacific
        stt_engine: google
        keys:
            GOOGLE_SPEECH: $YOUR_KEY_HERE

    """

    SLUG = 'google'

    def __init__(self, api_key=None, language='en-us'):
        # FIXME: get init args from config
        """
        Arguments:
        api_key - the public api key which allows access to Google APIs
        """
        self._logger = logging.getLogger(__name__)
        self._request_url = None
        self._language = None
        self._api_key = None
        self._http = requests.Session()
        self.language = language
        self.api_key = api_key

    @property
    def request_url(self):
        return self._request_url

    @property
    def language(self):
        return self._language

    @language.setter
    def language(self, value):
        self._language = value
        self._regenerate_request_url()

    @property
    def api_key(self):
        return self._api_key

    @api_key.setter
    def api_key(self, value):
        self._api_key = value
        self._regenerate_request_url()

    def _regenerate_request_url(self):
        if self.api_key and self.language:
            query = urllib.urlencode({'output': 'json',
                                      'client': 'chromium',
                                      'key': self.api_key,
                                      'lang': self.language,
                                      'maxresults': 6,
                                      'pfilter': 2})
            self._request_url = parse.urlunparse(
                ('https', 'www.google.com', '/speech-api/v2/recognize', '',
                 query, ''))
        else:
            self._request_url = None

    @classmethod
    def get_config(cls):
        # FIXME: Replace this as soon as we have a config module
        config = {}
        # HMM dir
        # Try to get hmm_dir from config
        profile_path = jasperpath.config('profile.yml')
        if os.path.exists(profile_path):
            with open(profile_path, 'r') as f:
                profile = yaml.safe_load(f)
                if 'keys' in profile and 'GOOGLE_SPEECH' in profile['keys']:
                    config['api_key'] = profile['keys']['GOOGLE_SPEECH']
        return config

    def transcribe(self, fp):
        """
        Performs STT via the Google Speech API, transcribing an audio file and
        returning an English string.

        Arguments:
        audio_file_path -- the path to the .wav file to be transcribed
        """

        if not self.api_key:
            self._logger.critical('API key missing, transcription request ' +
                                  'aborted.')
            return []
        elif not self.language:
            self._logger.critical('Language info missing, transcription ' +
                                  'request aborted.')
            return []

        wav = wave.open(fp, 'rb')
        frame_rate = wav.getframerate()
        wav.close()
        data = fp.read()

        headers = {'content-type': 'audio/l16; rate=%s' % frame_rate}
        r = self._http.post(self.request_url, data=data, headers=headers)
        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError:
            self._logger.critical('Request failed with http status %d',
                                  r.status_code)
            if r.status_code == requests.codes['forbidden']:
                self._logger.warning('Status 403 is probably caused by an ' +
                                     'invalid Google API key.')
            return []
        r.encoding = 'utf-8'
        try:
            # We cannot simply use r.json() because Google sends invalid json
            # (i.e. multiple json objects, seperated by newlines. We only want
            # the last one).
            response = json.loads(list(r.text.strip().split('\n', 1))[-1])
            if len(response['result']) == 0:
                # Response result is empty
                raise ValueError('Nothing has been transcribed.')
            results = [alt['transcript'] for alt
                       in response['result'][0]['alternative']]
        except ValueError as e:
            self._logger.warning('Empty response: %s', e.args[0])
            results = []
        except (KeyError, IndexError):
            self._logger.warning('Cannot parse response.', exc_info=True)
            results = []
        else:
            # Convert all results to uppercase
            results = tuple(result.upper() for result in results)
            self._logger.info('Transcribed: %r', results)
        return results

    @classmethod
    def is_available(cls):
        return diagnose.check_network_connection()


class AttSTT(AbstractSTTEngine):
    """
    Speech-To-Text implementation which relies on the AT&T Speech API.

    This implementation requires an AT&T app_key/app_secret to be present in
    profile.yml. Please sign up at http://developer.att.com/apis/speech and
    create a new app. You can then take the app_key/app_secret and put it into
    your profile.yml:
        ...
        stt_engine: att
        att-stt:
          app_key:    4xxzd6abcdefghijklmnopqrstuvwxyz
          app_secret: 6o5jgiabcdefghijklmnopqrstuvwxyz
    """

    SLUG = "att"

    def __init__(self, app_key, app_secret):
        self._logger = logging.getLogger(__name__)
        self._token = None
        self.app_key = app_key
        self.app_secret = app_secret

    @classmethod
    def get_config(cls):
        # FIXME: Replace this as soon as we have a config module
        config = {}
        # Try to get AT&T app_key/app_secret from config
        profile_path = jasperpath.config('profile.yml')
        if os.path.exists(profile_path):
            with open(profile_path, 'r') as f:
                profile = yaml.safe_load(f)
                if 'att-stt' in profile:
                    if 'app_key' in profile['att-stt']:
                        config['app_key'] = profile['att-stt']['app_key']
                    if 'app_secret' in profile['att-stt']:
                        config['app_secret'] = profile['att-stt']['app_secret']
        return config

    @property
    def token(self):
        if not self._token:
            headers = {'content-type': 'application/x-www-form-urlencoded',
                       'accept': 'application/json'}
            payload = {'client_id': self.app_key,
                       'client_secret': self.app_secret,
                       'scope': 'SPEECH',
                       'grant_type': 'client_credentials'}
            r = requests.post('https://api.att.com/oauth/v4/token',
                              data=payload,
                              headers=headers)
            self._token = r.json()['access_token']
        return self._token

    def transcribe(self, fp):
        data = fp.read()
        r = self._get_response(data)
        if r.status_code == requests.codes['unauthorized']:
            # Request token invalid, retry once with a new token
            self._logger.warning('OAuth access token invalid, generating a ' +
                                 'new one and retrying...')
            self._token = None
            r = self._get_response(data)
        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError:
            self._logger.critical('Request failed with response: %r',
                                  r.text,
                                  exc_info=True)
            return []
        except requests.exceptions.RequestException:
            self._logger.critical('Request failed.', exc_info=True)
            return []
        else:
            try:
                recognition = r.json()['Recognition']
                if recognition['Status'] != 'OK':
                    raise ValueError(recognition['Status'])
                results = [(x['Hypothesis'], x['Confidence'])
                           for x in recognition['NBest']]
            except ValueError as e:
                self._logger.debug('Recognition failed with status: %s',
                                   e.args[0])
                return []
            except KeyError:
                self._logger.critical('Cannot parse response.',
                                      exc_info=True)
                return []
            else:
                transcribed = [x[0].upper() for x in sorted(results,
                                                            key=lambda x: x[1],
                                                            reverse=True)]
                self._logger.info('Transcribed: %r', transcribed)
                return transcribed

    def _get_response(self, data):
        headers = {'authorization': 'Bearer %s' % self.token,
                   'accept': 'application/json',
                   'content-type': 'audio/wav'}
        return requests.post('https://api.att.com/speech/v3/speechToText',
                             data=data,
                             headers=headers)

    @classmethod
    def is_available(cls):
        return diagnose.check_network_connection()


class WitAiSTT(AbstractSTTEngine):
    """
    Speech-To-Text implementation which relies on the Wit.ai Speech API.

    This implementation requires an Wit.ai Access Token to be present in
    profile.yml. Please sign up at https://wit.ai and copy your instance
    token, which can be found under Settings in the Wit console to your
    profile.yml:
        ...
        stt_engine: witai
        witai-stt:
          access_token:    ERJKGE86SOMERANDOMTOKEN23471AB
    """

    SLUG = "witai"

    def __init__(self, access_token):
        self._logger = logging.getLogger(__name__)
        self.token = access_token

    @classmethod
    def get_config(cls):
        # FIXME: Replace this as soon as we have a config module
        config = {}
        # Try to get wit.ai Auth token from config
        profile_path = jasperpath.config('profile.yml')
        if os.path.exists(profile_path):
            with open(profile_path, 'r') as f:
                profile = yaml.safe_load(f)
                if 'witai-stt' in profile:
                    if 'access_token' in profile['witai-stt']:
                        config['access_token'] = \
                            profile['witai-stt']['access_token']
        return config

    @property
    def token(self):
        return self._token

    @token.setter
    def token(self, value):
        self._token = value
        self._headers = {'Authorization': 'Bearer %s' % self.token,
                         'accept': 'application/json',
                         'Content-Type': 'audio/wav'}

    @property
    def headers(self):
        return self._headers

    def transcribe(self, fp):
        data = fp.read()
        r = requests.post('https://api.wit.ai/speech?v=20150101',
                          data=data,
                          headers=self.headers)
        try:
            r.raise_for_status()
            text = r.json()['_text']
        except requests.exceptions.HTTPError:
            self._logger.critical('Request failed with response: %r',
                                  r.text,
                                  exc_info=True)
            return []
        except requests.exceptions.RequestException:
            self._logger.critical('Request failed.', exc_info=True)
            return []
        except ValueError as e:
            self._logger.critical('Cannot parse response: %s',
                                  e.args[0])
            return []
        except KeyError:
            self._logger.critical('Cannot parse response.',
                                  exc_info=True)
            return []
        else:
            transcribed = []
            if text:
                transcribed.append(text.upper())
            self._logger.info('Transcribed: %r', transcribed)
            return transcribed

    @classmethod
    def is_available(cls):
        return diagnose.check_network_connection()


def get_engine_by_slug(slug=None):
    """
    Returns:
        An STT Engine implementation available on the current platform

    Raises:
        ValueError if no speaker implementation is supported on this platform
    """

    if not slug or type(slug) is not str:
        raise TypeError("Invalid slug '%s'", slug)

    selected_engines = list(filter(lambda engine: hasattr(engine, "SLUG") and
                              engine.SLUG == slug, get_engines()))
    if len(selected_engines) == 0:
        raise ValueError("No STT engine found for slug '%s'" % slug)
    else:
        if len(selected_engines) > 1:
            print(("WARNING: Multiple STT engines found for slug '%s'. " +
                   "This is most certainly a bug.") % slug)
        engine = selected_engines[0]
        if not engine.is_available():
            raise ValueError(("STT engine '%s' is not available (due to " +
                              "missing dependencies, missing " +
                              "dependencies, etc.)") % slug)
        return engine


def get_engines():
    def get_subclasses(cls):
        subclasses = set()
        for subclass in cls.__subclasses__():
            subclasses.add(subclass)
            subclasses.update(get_subclasses(subclass))
        return subclasses
    return [tts_engine for tts_engine in
            list(get_subclasses(AbstractSTTEngine))
            if hasattr(tts_engine, 'SLUG') and tts_engine.SLUG]
