/**
 * What a staged attachment becomes in the message text.
 *
 * The composer stages pasted/dropped files as chips; the harness can only
 * receive the bytes through the host's upload verb. When that verb is wired,
 * each staged file is uploaded on send and the returned reference — a path
 * the server's ingest flow recognizes — is put into the message text, where
 * the model reads it. A file that could not be delivered is said honestly
 * rather than pretended delivered; without the verb, the note is all a page
 * can say, and that note must keep meaning "look at the thumbnail yourself".
 */

/** Compose the text a send delivers: what was typed, plus one reference per
 *  uploaded file (or the honest failure note for the ones that did not make
 *  it). Pure apart from the upload calls, so the composer's submit stays a
 *  thin shell around it and tests can drive it without a DOM. */
export async function composeMessageText(
  typed: string,
  files: File[],
  upload: ((file: File) => Promise<string | null>) | null,
): Promise<string> {
  if (!files.length) return typed;
  if (!upload) {
    /* no upload channel: say what arrived rather than pretend it was
       delivered — the person reads the thumbnails, the model gets the note */
    return typed
      || `[the user attached ${files.length} file(s): ${files.map((f) => f.name).join(", ")}]`;
  }
  const refs: string[] = [];
  const failed: string[] = [];
  for (const f of files) {
    const ref = await upload(f);
    if (ref) refs.push(ref);
    else failed.push(f.name || "a file");
  }
  const parts: string[] = [];
  if (typed) parts.push(typed);
  if (refs.length) parts.push(refs.join("\n"));
  if (failed.length) {
    parts.push(`[the user attached ${failed.join(", ")} but it could not be uploaded]`);
  }
  return parts.join("\n\n");
}