/**
 * Every About URL of an upload (D19), for the reviewer. A URL is a link only when
 * `referenceLink` accepts it (https, an ASCII/punycode DNS host, no `user@`, no IP); the host
 * is shown next to it in punycode so a look-alike name cannot hide, and the link carries
 * `rel="noopener noreferrer nofollow ugc"` and an external-link marker. Anything else (a
 * `javascript:` URL included) is plain text with the reason.
 */
import { Ban, ExternalLink } from "lucide-react";

import { displayText, referenceLink, UPLOADED_LINK_REL } from "./states";

export function UrlList({ urls }: { urls: readonly string[] }) {
  return (
    <section data-testid="plugin-urls" aria-labelledby="plugin-urls-title" className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3 rounded-2xl border bg-card p-5">
      <div className="grid gap-1">
        <h3 id="plugin-urls-title" className="font-display text-lg leading-tight font-semibold">
          Links in the About cards
        </h3>
        <p className="text-sm text-muted-foreground">Check where each one goes. The host name is shown as the browser sees it.</p>
      </div>
      {urls.length === 0 ? (
        <p className="text-sm text-muted-foreground">The About cards have no links.</p>
      ) : (
        <ul className="grid gap-2" aria-label="About links">
          {urls.map((url, i) => {
            const link = referenceLink(url);
            return (
              <li key={`${url}:${i}`} className="grid gap-0.5 border-t pt-2 first:border-t-0 first:pt-0" data-link-ok={link.ok ? "true" : "false"}>
                {link.ok ? (
                  <>
                    <a
                      href={link.href}
                      target="_blank"
                      rel={UPLOADED_LINK_REL}
                      referrerPolicy="no-referrer"
                      className="link inline-flex max-w-full items-start gap-1 font-mono text-xs [overflow-wrap:anywhere]"
                    >
                      <span className="min-w-0">{link.href}</span>
                      <ExternalLink aria-label="(opens an outside site)" className="mt-0.5 size-3 flex-none" />
                    </a>
                    <span className="text-xs text-muted-foreground">
                      Host <span className="font-mono font-semibold text-foreground">{link.host}</span>
                    </span>
                  </>
                ) : (
                  <>
                    <span className="inline-flex max-w-full items-start gap-1 font-mono text-xs [overflow-wrap:anywhere]">
                      <Ban aria-hidden className="mt-0.5 size-3 flex-none text-destructive" />
                      <span className="min-w-0">{displayText(url)}</span>
                    </span>
                    <span className="text-xs text-destructive">Not a link: {link.reason}</span>
                  </>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
