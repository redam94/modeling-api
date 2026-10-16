/** The read-only Monaco view of the spec in the "View as YAML" drawer (lazy: Monaco is large). */
import { YamlEditor } from "@/features/editor/yaml-editor";

export default function YamlView({ value, path }: { value: string; path: string }) {
  return <YamlEditor path={path} value={value} readOnly height="min(60vh, 32rem)" ariaLabel="Model specification (read-only)" />;
}
