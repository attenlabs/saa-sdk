import { forwardSpeechReadyToOpenAIRealtime } from "@attenlabs/saa-gate";

/**
 * Route only SAA-approved utterances into an OpenAI Realtime data channel.
 *
 * Assumption: your Realtime session is configured for manual audio-buffer
 * control, or VAD is kept but automatic response creation is disabled. That
 * lets SAA be the upstream addressee gate instead of sending all room audio.
 */
export function createOpenAIRealtimeSaaRouter({ dataChannel, response }) {
  if (!dataChannel || typeof dataChannel.send !== "function") {
    throw new TypeError("dataChannel with send(string) is required");
  }

  let sequence = 0;
  return {
    routeSpeechReady(speech) {
      sequence += 1;
      forwardSpeechReadyToOpenAIRealtime(dataChannel, speech, {
        eventIdPrefix: `saa-utt-${sequence}`,
        response,
      });
    },
  };
}
