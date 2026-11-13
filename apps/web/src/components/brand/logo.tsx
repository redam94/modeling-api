/**
 * The mark: three bars that narrow to a point estimate (a posterior fan, abstracted). The top
 * bar is the data blue, the middle bar is the ink (the text colour around it) and the last bar is
 * the data orange.
 *
 * No imports: the share link page and the client portal use this module too, and their bundles
 * may load nothing from the signed-in app.
 */
export function Logo({ className = "size-6" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={`${className} shrink-0`} aria-hidden>
      <rect x="2" y="4" width="20" height="4" rx="2" className="fill-series-1" />
      <rect x="5" y="10" width="14" height="4" rx="2" className="fill-current" />
      <rect x="9" y="16" width="6" height="4" rx="2" className="fill-series-2" />
    </svg>
  );
}

/** The mark with the product name beside it. */
export function Wordmark({ className = "size-6" }: { className?: string }) {
  return (
    <span className="flex items-center gap-2.5">
      <Logo className={className} />
      <span className="font-display text-[15px] leading-none font-semibold tracking-[-0.02em]">modeling-api</span>
    </span>
  );
}
