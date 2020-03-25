"""Hermes implementation of RhasspyCore"""
import asyncio
import io
import logging
import os
import ssl
import typing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import aiohttp
import paho.mqtt.client as mqtt
import rhasspynlu
from paho.mqtt.matcher import MQTTMatcher
from rhasspyhermes.asr import (
    AsrAudioCaptured,
    AsrError,
    AsrStartListening,
    AsrStopListening,
    AsrTextCaptured,
    AsrToggleOff,
    AsrToggleOn,
    AsrTrain,
    AsrTrainSuccess,
)
from rhasspyhermes.audioserver import (
    AudioDeviceMode,
    AudioDevices,
    AudioFrame,
    AudioGetDevices,
    AudioPlayBytes,
    AudioPlayFinished,
    AudioSessionFrame,
    AudioSummary,
)
from rhasspyhermes.base import Message
from rhasspyhermes.client import GeneratorType, HermesClient
from rhasspyhermes.dialogue import DialogueSessionStarted
from rhasspyhermes.g2p import G2pPhonemes, G2pPronounce
from rhasspyhermes.nlu import (
    NluError,
    NluIntent,
    NluIntentNotRecognized,
    NluQuery,
    NluTrain,
    NluTrainSuccess,
)
from rhasspyhermes.tts import GetVoices, TtsSay, TtsSayFinished, Voices
from rhasspyhermes.wake import (
    GetHotwords,
    HotwordDetected,
    Hotwords,
    HotwordToggleOff,
    HotwordToggleOn,
)
from rhasspyprofile import Profile

from .train import sentences_to_graph
from .utils import get_ini_paths

_LOGGER = logging.getLogger("rhasspyserver_hermes")

# -----------------------------------------------------------------------------


@dataclass
class TrainingFailedException(Exception):
    """Raised when training fails."""

    reason: str

    def __str__(self):
        return self.reason


# -----------------------------------------------------------------------------


class RhasspyCore(HermesClient):
    """Core Rhasspy functionality, abstracted over MQTT Hermes services"""

    def __init__(
        self,
        profile_name: str,
        system_profiles_dir: typing.Optional[typing.Union[str, Path]],
        user_profiles_dir: typing.Union[str, Path],
        host: typing.Optional[str] = None,
        port: typing.Optional[int] = None,
        username: typing.Optional[str] = None,
        password: typing.Optional[str] = None,
        local_mqtt_port: int = 12183,
        loop=None,
        connection_retries: int = 10,
        retry_seconds: float = 1,
        message_timeout_seconds: float = 30,
        training_timeout_seconds: float = 600,
        certfile: typing.Optional[str] = None,
        keyfile: typing.Optional[str] = None,
    ):
        # Load profile
        self.profile_name = profile_name

        if system_profiles_dir:
            system_profiles_dir = Path(system_profiles_dir)

        self.user_profiles_dir = Path(user_profiles_dir)

        self.profile = Profile(
            self.profile_name, system_profiles_dir, self.user_profiles_dir
        )

        self.defaults = Profile.load_defaults(self.profile.system_profiles_dir)

        # Look up siteId(s) in profile
        siteIds = str(self.profile.get("mqtt.site_id", "")).split(",")

        super().__init__(
            "rhasspyserver_hermes", mqtt.Client(), siteIds=siteIds, loop=loop
        )

        self.subscribe(
            HotwordDetected,
            AsrTextCaptured,
            NluIntent,
            NluIntentNotRecognized,
            AsrAudioCaptured,
            AudioSummary,
        )

        # Event loop
        self.loop = loop or asyncio.get_event_loop()

        # Default timeout for response messages
        self.message_timeout_seconds = message_timeout_seconds
        self.training_timeout_seconds = training_timeout_seconds

        remote_mqtt = str(self.profile.get("mqtt.enabled", False)).lower() == "true"
        if remote_mqtt:
            # External broker
            self.host = host or str(self.profile.get("mqtt.host", "localhost"))
            self.port = port or int(self.profile.get("mqtt.port", 1883))
            self.username = username or str(self.profile.get("mqtt.username", ""))
            self.password = password or str(self.profile.get("mqtt.password", ""))
        else:
            # Internal broker
            self.host = host or "localhost"
            self.port = port or local_mqtt_port
            self.username = ""
            self.password = ""

        if self.username:
            # MQTT username/password
            self.mqtt_client.username_pw_set(self.username, self.password)

        # MQTT retries
        self.connection_retries = connection_retries
        self.retry_seconds = retry_seconds

        # id -> matcher
        self.handler_matchers: typing.Dict[str, MQTTMatcher] = {}

        # id -> async queue
        self.handler_queues: typing.Dict[str, asyncio.Queue] = {}

        # External message queues
        self.message_queues: typing.Set[asyncio.Queue] = set()

        # Shared aiohttp client session (enable SSL)
        self.ssl_context = ssl.SSLContext()
        if certfile:
            self.ssl_context.load_cert_chain(certfile, keyfile)

        self._http_session: typing.Optional[
            aiohttp.ClientSession
        ] = aiohttp.ClientSession()

        # Cached wake word IDs for sessions
        self.session_wakewordIds: typing.Dict[str, str] = {}

        # Holds last voice command
        self.last_audio_captured: typing.Optional[AsrAudioCaptured] = None

        # Enable/disable playing sounds on events
        self.sounds_enabled = True

        self.asr_system = self.profile.get("speech_to_text.system", "dummy")
        self.dialogue_system = self.profile.get("dialogue.system", "dummy")

    @property
    def http_session(self) -> aiohttp.ClientSession:
        """Get HTTP client session."""
        if self._http_session is None:
            self._http_session = aiohttp.ClientSession()

        return self._http_session

    # -------------------------------------------------------------------------

    async def start(self):
        """Connect to MQTT broker"""
        _LOGGER.debug("Starting core")

        # Connect to MQTT broker
        retries = 0
        connected = False
        while retries < self.connection_retries:
            try:
                _LOGGER.debug(
                    "Connecting to %s:%s (retries: %s)", self.host, self.port, retries
                )
                self.mqtt_client.connect(self.host, self.port)
                self.mqtt_client.loop_start()
                await self.mqtt_connected_event.wait()
                connected = True
                break
            except Exception:
                _LOGGER.exception("mqtt connect")
                retries += 1
                await asyncio.sleep(self.retry_seconds)

        if not connected:
            _LOGGER.fatal(
                "Failed to connect to MQTT broker (%s:%s)", self.host, self.port
            )
            raise RuntimeError("Failed to connect to MQTT broker")

    async def shutdown(self):
        """Disconnect from MQTT broker"""
        _LOGGER.debug("Shutting down core")

        # Shut down MQTT client
        self.mqtt_client.loop_stop()

        # Shut down HTTP session
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    # -------------------------------------------------------------------------

    async def train(self):
        """Send an NLU query and wait for intent or not recognized"""

        # Load sentences.ini files
        sentences_ini = self.profile.read_path(
            self.profile.get("speech_to_text.sentences_ini", "sentences.ini")
        )
        sentences_dir: typing.Optional[Path] = self.profile.read_path(
            self.profile.get("speech_to_text.sentences_dir", "intents")
        )

        assert sentences_dir is not None
        if not sentences_dir.is_dir():
            sentences_dir = None

        ini_paths = get_ini_paths(sentences_ini, sentences_dir)
        _LOGGER.debug("Loading sentences from %s", ini_paths)

        sentences_dict = {str(p): p.read_text() for p in ini_paths}

        # Load settings
        language = self.profile.get("language", "en")
        dictionary_casing = self.profile.get(
            "speech_to_text.dictionary_casing", "ignore"
        ).lower()
        word_transform = None
        if dictionary_casing == "upper":
            word_transform = str.upper
        elif dictionary_casing == "lower":
            word_transform = str.lower

        slots_dir = self.profile.write_path(
            self.profile.get("speech_to_text.slots_dir", "slots")
        )
        system_slots_dir = (
            self.profile.system_profiles_dir / self.profile.name / "slots"
        )
        slot_programs_dir = self.profile.write_path(
            self.profile.get("speech_to_text.slot_programs_dir", "slot_programs")
        )
        system_slot_programs_dir = (
            self.profile.system_profiles_dir / self.profile.name / "slot_programs"
        )

        # Convert to graph
        _LOGGER.debug("Generating intent graph")

        intent_graph = sentences_to_graph(
            sentences_dict,
            slots_dirs=[slots_dir, system_slots_dir],
            slot_programs_dirs=[slot_programs_dir, system_slot_programs_dir],
            language=language,
            word_transform=word_transform,
        )

        # Convert to gzipped pickle
        graph_path = self.profile.write_path(
            self.profile.get("intent.fsticuffs.intent_json", "intent_graph.pickle.gz")
        )
        _LOGGER.debug("Writing %s", graph_path)
        with open(graph_path, mode="wb") as graph_file:
            rhasspynlu.graph_to_gzip_pickle(intent_graph, graph_file)

        _LOGGER.debug("Finished writing %s", graph_path)

        # Send to ASR/NLU systems
        speech_system = self.profile.get("speech_to_text.system", "dummy")
        has_speech = speech_system != "dummy"

        intent_system = self.profile.get("intent.system", "dummy")
        has_intent = intent_system != "dummy"

        if has_speech or has_intent:
            request_id = str(uuid4())

            def handle_train():
                asr_response = None if has_speech else True
                nlu_response = None if has_intent else True

                while True:
                    _, message = yield

                    if isinstance(message, NluTrainSuccess) and (
                        message.id == request_id
                    ):
                        nlu_response = message
                    elif isinstance(message, AsrTrainSuccess) and (
                        message.id == request_id
                    ):
                        asr_response = message
                    if isinstance(message, NluError) and (
                        message.sessionId == request_id
                    ):
                        nlu_response = message
                    elif isinstance(message, AsrError) and (
                        message.sessionId == request_id
                    ):
                        asr_response = message

                    if asr_response and nlu_response:
                        return [asr_response, nlu_response]

            messages: typing.List[
                typing.Tuple[
                    typing.Union[NluTrain, AsrTrain], typing.Dict[str, typing.Any]
                ],
            ] = []

            message_types: typing.List[typing.Type[Message]] = []

            if has_speech:
                # Request ASR training
                messages.append(
                    (
                        AsrTrain(id=request_id, graph_path=str(graph_path.absolute())),
                        {"siteId": self.siteId},
                    )
                )
                message_types.extend([AsrTrainSuccess, AsrError])

            if has_intent:
                # Request NLU training
                messages.append(
                    (
                        NluTrain(id=request_id, graph_path=str(graph_path.absolute())),
                        {"siteId": self.siteId},
                    )
                )
                message_types.extend([NluTrainSuccess, NluError])

            # Expecting only a single result
            result = None
            async for response in self.publish_wait(
                handle_train(),
                messages,
                message_types,
                timeout_seconds=self.training_timeout_seconds,
            ):
                result = response

            # Check result
            assert isinstance(result, list), f"Expected list, got {result}"
            asr_response, nlu_response = result

            if isinstance(asr_response, AsrError):
                _LOGGER.error(asr_response)
                raise TrainingFailedException(reason=asr_response.error)

            if isinstance(nlu_response, NluError):
                _LOGGER.error(nlu_response)
                raise TrainingFailedException(reason=nlu_response.error)

            return result

        return None

    # -------------------------------------------------------------------------

    async def recognize_intent(
        self, text: str
    ) -> typing.Union[NluIntent, NluIntentNotRecognized]:
        """Send an NLU query and wait for intent or not recognized"""
        if self.profile.get("intent.system", "dummy") == "dummy":
            _LOGGER.debug("No intent system configured")
            return NluIntentNotRecognized(input="")

        nlu_id = str(uuid4())
        query = NluQuery(id=nlu_id, input=text, siteId=self.siteId, sessionId=nlu_id)

        def handle_intent():
            while True:
                _, message = yield

                if isinstance(
                    message, (NluIntent, NluIntentNotRecognized, NluError)
                ) and (message.sessionId == nlu_id):
                    return message

        messages = [query]
        message_types: typing.List[typing.Type[Message]] = [
            NluIntent,
            NluIntentNotRecognized,
            NluError,
        ]

        # Expecting only a single result
        result = None
        async for response in self.publish_wait(
            handle_intent(), messages, message_types
        ):
            result = response

        if isinstance(result, NluError):
            _LOGGER.debug(result)
            raise RuntimeError(result.error)

        assert isinstance(result, (NluIntent, NluIntentNotRecognized))
        return result

    # -------------------------------------------------------------------------

    async def speak_sentence(
        self,
        sentence: str,
        language: typing.Optional[str] = None,
        capture_audio: bool = False,
        siteId: typing.Optional[str] = None,
    ) -> typing.Tuple[TtsSayFinished, typing.Optional[AudioPlayBytes]]:
        """Speak a sentence using text to speech."""
        if self.profile.get("text_to_speech.system", "dummy") == "dummy":
            _LOGGER.debug("No text to speech system configured")
            return (TtsSayFinished(), None)

        siteId = siteId or self.siteId
        tts_id = str(uuid4())

        def handle_finished():
            say_finished: typing.Optional[TtsSayFinished] = None
            play_bytes: typing.Optional[
                AudioPlayBytes
            ] = None if capture_audio else True

            while True:
                topic, message = yield

                if isinstance(message, TtsSayFinished) and (message.id == tts_id):
                    say_finished = message
                elif isinstance(message, AudioPlayBytes):
                    requestId = AudioPlayBytes.get_requestId(topic)
                    if requestId == tts_id:
                        play_bytes = message

                if say_finished and play_bytes:
                    return (say_finished, play_bytes)

        say = TtsSay(id=tts_id, text=sentence, siteId=siteId)
        if language:
            say.lang = language

        messages = [say]
        message_types: typing.List[typing.Type[Message]] = [
            TtsSayFinished,
            AudioPlayBytes,
        ]

        # Expecting only a single result
        result = None
        async for response in self.publish_wait(
            handle_finished(), messages, message_types
        ):
            result = response

        assert isinstance(result, tuple), f"Expected tuple, got {result}"
        say_response, play_response = result
        assert isinstance(say_response, TtsSayFinished)

        if capture_audio:
            assert isinstance(play_response, AudioPlayBytes)

        return typing.cast(
            typing.Tuple[TtsSayFinished, typing.Optional[AudioPlayBytes]], result
        )

    # -------------------------------------------------------------------------

    async def transcribe_wav(
        self, wav_bytes: bytes, frames_per_chunk: int = 4096, sendAudioCaptured=True
    ) -> AsrTextCaptured:
        """Transcribe WAV data"""
        if self.profile.get("speech_to_text.system", "dummy") == "dummy":
            _LOGGER.debug("No speech to text system configured")
            return AsrTextCaptured(text="", likelihood=0, seconds=0)

        sessionId = str(uuid4())

        def handle_captured():
            while True:
                _, message = yield

                if isinstance(message, (AsrTextCaptured, AsrError)) and (
                    message.sessionId == sessionId
                ):
                    return message

        def messages():
            yield AsrStartListening(
                siteId=self.siteId,
                sessionId=sessionId,
                stopOnSilence=False,
                sendAudioCaptured=sendAudioCaptured,
            )

            # Break WAV into chunks
            num_bytes_sent: int = 0
            with io.BytesIO(wav_bytes) as wav_buffer:
                for wav_chunk in AudioFrame.iter_wav_chunked(
                    wav_buffer, frames_per_chunk
                ):
                    num_bytes_sent += len(wav_chunk)
                    yield (
                        AudioSessionFrame(wav_bytes=wav_chunk),
                        {"siteId": self.siteId, "sessionId": sessionId},
                    )

            _LOGGER.debug("Sent %s byte(s) of WAV data", num_bytes_sent)
            yield AsrStopListening(siteId=self.siteId, sessionId=sessionId)

        message_types: typing.List[typing.Type[Message]] = [AsrTextCaptured, AsrError]

        # Expecting only a single result
        result = None
        async for response in self.publish_wait(
            handle_captured(), messages(), message_types
        ):
            result = response

        if isinstance(result, AsrError):
            _LOGGER.debug(result)
            raise RuntimeError(result.error)

        assert isinstance(result, AsrTextCaptured)
        return result

    # -------------------------------------------------------------------------

    async def play_wav_data(
        self, wav_bytes: bytes, siteId: typing.Optional[str] = None
    ) -> AudioPlayFinished:
        """Play WAV data through speakers."""
        if self.profile.get("sounds.system", "dummy") == "dummy":
            _LOGGER.debug("No audio output system configured")
            return AudioPlayFinished()

        siteId = siteId or self.siteId
        requestId = str(uuid4())

        def handle_finished():
            while True:
                _, message = yield

                if isinstance(message, AudioPlayFinished) and (message.id == requestId):
                    return message

        def messages():
            yield (
                AudioPlayBytes(wav_bytes=wav_bytes),
                {"siteId": siteId, "requestId": requestId},
            )

        message_types: typing.List[typing.Type[Message]] = [AudioPlayFinished]

        # Disable hotword/ASR
        self.publish(HotwordToggleOff(siteId=siteId))
        self.publish(AsrToggleOff(siteId=siteId))

        try:
            # Expecting only a single result
            result = None
            async for response in self.publish_wait(
                handle_finished(), messages(), message_types
            ):
                result = response

            assert isinstance(result, AudioPlayFinished)
            return result
        finally:
            # Enable hotword/ASR
            self.publish(HotwordToggleOn(siteId=siteId))
            self.publish(AsrToggleOn(siteId=siteId))

    # -------------------------------------------------------------------------

    async def get_word_pronunciations(
        self, words: typing.Iterable[str], num_guesses: int = 5
    ) -> G2pPhonemes:
        """Look up or guess word phonetic pronunciations."""
        requestId = str(uuid4())

        def handle_finished():
            while True:
                _, message = yield

                if isinstance(message, G2pPhonemes) and (message.id == requestId):
                    return message

        messages = [
            G2pPronounce(
                words=list(words),
                numGuesses=num_guesses,
                id=requestId,
                siteId=self.siteId,
            )
        ]
        message_types: typing.List[typing.Type[Message]] = [G2pPhonemes]

        # Expecting only a single result
        result = None
        async for response in self.publish_wait(
            handle_finished(), messages, message_types
        ):
            result = response

        assert isinstance(result, G2pPhonemes)
        return result

    # -------------------------------------------------------------------------

    async def get_microphones(self, test: bool = False) -> AudioDevices:
        """Get available microphones and optionally test them."""
        if self.profile.get("microphone.system", "dummy") == "dummy":
            _LOGGER.warning("Microphone disabled. Cannot get available input devices.")
            return AudioDevices()

        requestId = str(uuid4())

        def handle_finished():
            while True:
                _, message = yield

                if isinstance(message, AudioDevices) and (message.id == requestId):
                    return message

        messages = [
            AudioGetDevices(
                id=requestId,
                siteId=self.siteId,
                modes=[AudioDeviceMode.INPUT],
                test=test,
            )
        ]
        message_types: typing.List[typing.Type[Message]] = [AudioDevices]

        # Expecting only a single result
        result = None
        async for response in self.publish_wait(
            handle_finished(), messages, message_types
        ):
            result = response

        assert isinstance(result, AudioDevices)
        return result

    async def get_speakers(self) -> AudioDevices:
        """Get available speakers."""
        if self.profile.get("sounds.system", "dummy") == "dummy":
            _LOGGER.warning("Speakers disabled. Cannot get available output devices.")
            return AudioDevices()

        requestId = str(uuid4())

        def handle_finished():
            while True:
                _, message = yield

                if isinstance(message, AudioDevices) and (message.id == requestId):
                    return message

        messages = [
            AudioGetDevices(
                id=requestId, siteId=self.siteId, modes=[AudioDeviceMode.OUTPUT]
            )
        ]
        message_types: typing.List[typing.Type[Message]] = [AudioDevices]

        # Expecting only a single result
        result = None
        async for response in self.publish_wait(
            handle_finished(), messages, message_types
        ):
            result = response

        assert isinstance(result, AudioDevices)
        return result

    async def get_hotwords(self) -> Hotwords:
        """Get available hotwords."""
        if self.profile.get("wake.system", "dummy") == "dummy":
            _LOGGER.warning(
                "Wake word detection disabled. Cannot get available wake words."
            )
            return Hotwords()

        requestId = str(uuid4())

        def handle_finished():
            while True:
                _, message = yield

                if isinstance(message, Hotwords) and (message.id == requestId):
                    return message

        messages = [GetHotwords(id=requestId, siteId=self.siteId)]
        message_types: typing.List[typing.Type[Message]] = [Hotwords]

        # Expecting only a single result
        result = None
        async for response in self.publish_wait(
            handle_finished(), messages, message_types
        ):
            result = response

        assert isinstance(result, Hotwords)
        return result

    async def get_voices(self) -> Voices:
        """Get available voices for text to speech system."""
        if self.profile.get("text_to_speech.system", "dummy") == "dummy":
            _LOGGER.warning("Text to speech disabled. Cannot get available voices.")
            return Voices()

        requestId = str(uuid4())

        def handle_finished():
            while True:
                _, message = yield

                if isinstance(message, Voices) and (message.id == requestId):
                    return message

        messages = [GetVoices(id=requestId, siteId=self.siteId)]
        message_types: typing.List[typing.Type[Message]] = [Voices]

        # Expecting only a single result
        result = None
        async for response in self.publish_wait(
            handle_finished(), messages, message_types
        ):
            result = response

        assert isinstance(result, Voices)
        return result

    # -------------------------------------------------------------------------

    async def maybe_play_sound(
        self, sound_name: str, siteId: typing.Optional[str] = None
    ):
        """Play WAV sound through audio out if it exists."""
        sound_system = self.profile.get("sounds.system", "dummy")
        if (not self.sounds_enabled) or (sound_system == "dummy"):
            # No feedback sounds
            _LOGGER.debug("Sounds disabled (system=%s)", sound_system)
            return

        wav_path_str = os.path.expandvars(self.profile.get(f"sounds.{sound_name}", ""))
        if wav_path_str:
            wav_path = self.profile.read_path(wav_path_str)
            if not wav_path.is_file():
                _LOGGER.error("WAV does not exist: %s", str(wav_path))
                return

            _LOGGER.debug("Playing WAV %s", str(wav_path))
            await self.play_wav_data(wav_path.read_bytes(), siteId=siteId)

    # -------------------------------------------------------------------------
    # Supporting Functions
    # -------------------------------------------------------------------------

    async def publish_wait(
        self,
        handler,
        messages: typing.Sequence[
            typing.Union[Message, typing.Tuple[Message, typing.Dict[str, typing.Any]]]
        ],
        message_types: typing.List[typing.Type[Message]],
        timeout_seconds: typing.Optional[float] = None,
    ):
        """Publish messages and wait for responses."""
        timeout_seconds = timeout_seconds or self.message_timeout_seconds

        # Start generator
        handler.send(None)

        handler_id = str(uuid4())
        handler_matcher = MQTTMatcher()

        self.handler_matchers[handler_id] = handler_matcher
        self.handler_queues[handler_id] = asyncio.Queue()

        # Subscribe to requested message types
        self.subscribe(*message_types)
        for message_type in message_types:
            # Register handler for each message topic
            handler_matcher[message_type.topic()] = handler

        # Publish messages
        for maybe_message in messages:
            if isinstance(maybe_message, Message):
                # Just a message
                self.publish(maybe_message)
            else:
                # Message and keyword arguments
                message, kwargs = maybe_message
                self.publish(message, **kwargs)

        # Wait for response or timeout
        result_awaitable = self.handler_queues[handler_id].get()

        try:
            if timeout_seconds > 0:
                # With timeout
                _, result = await asyncio.wait_for(result_awaitable, timeout_seconds)
            else:
                # No timeout
                _, result = await result_awaitable

            yield result
        finally:
            # Remove queue
            self.handler_queues.pop(handler_id)

    # -------------------------------------------------------------------------

    def handle_message(self, topic: str, message: Message):
        """Send matching messages to waiting handlers."""
        if message.__class__.is_binary_payload():
            _LOGGER.debug(
                "<- %s(%s byte(s))", message.__class__.__name__, len(message.payload())
            )
        else:
            _LOGGER.debug("<- %s", message)

        for handler_id in list(self.handler_matchers):
            handler_matcher = self.handler_matchers[handler_id]

            for handler in handler_matcher.iter_match(topic):
                try:
                    # Run handler
                    _LOGGER.debug(
                        "Handling %s (topic=%s, id=%s)",
                        message.__class__.__name__,
                        topic,
                        handler_id,
                    )

                    try:
                        result = handler.send((topic, message))
                        done = False
                    except StopIteration as e:
                        result = e.value
                        done = True

                    if done:
                        # Message has satistfied handler
                        self.handler_matchers.pop(handler_id)

                    # Signal other thread
                    if done or (result is not None):
                        self.loop.call_soon_threadsafe(
                            self.handler_queues[handler_id].put_nowait, (done, result)
                        )
                except Exception:
                    _LOGGER.exception("handle_message")

    # -------------------------------------------------------------------------

    async def on_message(
        self,
        message: Message,
        siteId: typing.Optional[str] = None,
        sessionId: typing.Optional[str] = None,
        topic: typing.Optional[str] = None,
    ) -> GeneratorType:
        """Received message from MQTT broker."""
        assert topic, "Missing topic"

        if isinstance(message, AsrError):
            # ASR service error
            self.handle_message(topic, message)
        elif isinstance(message, AsrTextCaptured):
            # Successful transcription
            wakewordId = self.session_wakewordIds.pop(message.sessionId, "default")

            self.handle_message(topic, message)

            # Report to websockets
            for queue in self.message_queues:
                queue.put_nowait(("text", message, wakewordId))

            # Play recorded sound
            await self.maybe_play_sound("recorded", siteId=message.siteId)
        elif isinstance(message, AudioDevices):
            # Microphones or speakers
            self.handle_message(topic, message)
        elif isinstance(message, AudioPlayBytes):
            # Request to play audio
            assert siteId, "Missing siteId"
            self.handle_message(topic, message)
        elif isinstance(message, AudioPlayFinished):
            # Audio finished playing
            assert siteId, "Missing siteId"
            self.handle_message(topic, message)
        elif isinstance(message, AudioSummary):
            # Audio summary statistics
            assert siteId, "Missing siteId"

            # Report to websockets
            for queue in self.message_queues:
                queue.put_nowait(("audiosummary", message))
        elif isinstance(message, DialogueSessionStarted):
            # Dialogue session started
            self.handle_message(topic, message)
        elif isinstance(message, NluError):
            # NLU service error
            self.handle_message(topic, message)
        elif isinstance(message, NluIntent):
            # Successful intent recognition
            self.handle_message(topic, message)

            # Report to websockets
            for queue in self.message_queues:
                queue.put_nowait(("intent", message))
        elif isinstance(message, G2pPhonemes):
            # Word pronunciations
            self.handle_message(topic, message)
        elif isinstance(message, Hotwords):
            # Hotword list
            self.handle_message(topic, message)
        elif isinstance(message, NluIntentNotRecognized):
            # Failed intent recognition
            self.handle_message(topic, message)

            # Report to websockets
            for queue in self.message_queues:
                queue.put_nowait(("intent", message))

            # Play error sound
            await self.maybe_play_sound("error", siteId=message.siteId)
        elif isinstance(message, TtsSayFinished):
            # Text to speech complete
            self.handle_message(topic, message)
        elif isinstance(message, AsrTrainSuccess):
            # ASR training success
            assert siteId, "Missing siteId"
            self.handle_message(topic, message)
        elif isinstance(message, AsrAudioCaptured):
            # Audio data from ASR session
            assert siteId, "Missing siteId"
            self.last_audio_captured = message
            self.handle_message(topic, message)
        elif isinstance(message, NluTrainSuccess):
            # NLU training success
            assert siteId, "Missing siteId"
            self.handle_message(topic, message)
        elif isinstance(message, HotwordDetected):
            # Hotword detected
            wakewordId = HotwordDetected.get_wakewordId(topic)

            # Cache wake word ID for session
            self.session_wakewordIds[message.sessionId] = wakewordId

            self.handle_message(topic, message)

            # Report to websockets
            for queue in self.message_queues:
                queue.put_nowait(("wake", message, wakewordId))

            # Warn user if they're expected wake -> ASR -> NLU workflow
            if (self.dialogue_system == "dummy") and (self.asr_system != "dummy"):
                _LOGGER.warning(
                    "Dialogue management is disabled. ASR will NOT be automatically enabled."
                )

            # Play wake sound
            await self.maybe_play_sound("wake", siteId=message.siteId)
        elif isinstance(message, Voices):
            # Text to speech voices
            self.handle_message(topic, message)
        else:
            _LOGGER.warning("Unexpected message: %s", message)

        # Mark as async generator
        yield None

    async def on_raw_message(self, topic: str, payload: bytes):
        """Handle raw MQTT messages."""
        # Forward to external message queues
        for queue in self.message_queues:
            queue.put_nowait(("mqtt", topic, payload))
