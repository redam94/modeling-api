// Side-effect and deep-import entry points of monaco-editor that ship without typings.
declare module "monaco-editor/esm/vs/editor/editor.all.js";
declare module "monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution.js";
declare module "monaco-editor/esm/vs/editor/editor.api.js" {
  export * from "monaco-editor";
}
declare module "monaco-editor/esm/vs/editor/editor.worker.js?worker" {
  const WorkerFactory: new () => Worker;
  export default WorkerFactory;
}
