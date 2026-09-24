// Server-profile selection + URL wiring

/**
 * The server_profile this session requests, or undefined for the server
 * default. 
 * 
 * `enableVideo === false` selects "audio_only" 
 * "default" is no-op
 */
export function effectiveServerProfile(
  serverProfile: string | undefined,
  enableVideo: boolean,
): string | undefined {
  const prof = serverProfile != null ? serverProfile : enableVideo ? undefined : "audio_only";
  return prof && prof !== "default" ? prof : undefined;
}

/**
 * Direct ws(s):// mode for applying server_profile to the URL query
 */
export function applyServerProfileToWsUrl(
  url: string,
  serverProfile: string | undefined,
  enableVideo: boolean,
): string {
  const profile = effectiveServerProfile(serverProfile, enableVideo);
  if (!profile) return url;
  // inferred profile defers to a server_profile already in the URL
  if (serverProfile == null && new URL(url).searchParams.has("server_profile")) {
    return url;
  }
  const u = new URL(url);
  u.searchParams.set("server_profile", profile);
  return u.toString();
}

/**
 * Direct ws(s):// mode for opting into utterance handling
 */
export function applyUtteranceHandlingToWsUrl(url: string, enabled: boolean): string {
  if (!enabled) return url;
  const u = new URL(url);
  u.searchParams.set("utterance_handling", "1");
  return u.toString();
}

/**
 * Broker mode — the JSON body for POST /allocate
 *
 * Returns undefined when neither a profile nor utterance handling is selected
 * (legacy empty body).
 */
export function allocateBody(
  serverProfile: string | undefined,
  enableVideo: boolean,
  utteranceHandling = false,
): string | undefined {
  const profile = effectiveServerProfile(serverProfile, enableVideo);
  const body: Record<string, unknown> = {};
  if (profile) body.server_profile = profile;
  if (utteranceHandling) body.utterance_handling = true;
  return Object.keys(body).length ? JSON.stringify(body) : undefined;
}
