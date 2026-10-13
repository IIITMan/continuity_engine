"""Voice contracts and optional host-driven audio pipeline."""
from .voice import *
from .pipeline import PCM16EnergyVAD, VoiceConversationPipeline, VoiceTurnResult
from .command_adapters import CommandAdapterError, JsonCommandSpeechRecognizer, JsonCommandTextToSpeech

from .http_adapters import (AudioSink, OpenAICompatibleSpeechRecognizer, OpenAICompatibleTextToSpeech,
                            SubprocessAudioSink, VoiceProviderConfigurationError, VoiceProviderResponseError)
