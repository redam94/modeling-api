import { Badge } from "@/components/ui/primitives";

/** "key=value" badges of a run's tags, sorted by key; `max` caps them with a "+n" badge. */
export function TagBadges({ tags, max }: { tags: Record<string, string>; max?: number }) {
  const entries = Object.entries(tags).sort(([a], [b]) => a.localeCompare(b));
  if (!entries.length) return null;
  const shown = max !== undefined ? entries.slice(0, max) : entries;
  const rest = entries.length - shown.length;
  return (
    <span className="inline-flex max-w-full flex-wrap gap-1" aria-label="Tags">
      {shown.map(([k, v]) => (
        <Badge key={k} variant="outline" className="max-w-full font-normal [overflow-wrap:anywhere] whitespace-normal" title={`${k}=${v}`}>
          {v ? `${k}=${v}` : k}
        </Badge>
      ))}
      {rest > 0 ? (
        <Badge variant="secondary" className="font-normal" title={entries.slice(shown.length).map(([k, v]) => `${k}=${v}`).join("\n")}>
          +{rest}
        </Badge>
      ) : null}
    </span>
  );
}
