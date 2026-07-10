// F006 — Shell feature entry. Renders the Settings pane composed of the
// shell-feature components. Splash and FilePicker are exported so other
// features (corpus drag-and-drop, watch) can reuse them.
export { default } from "./AppShellSettings";
export { ProcessHealthIndicator } from "./ProcessHealthIndicator";
export { SplashScreen } from "./SplashScreen";
export { FilePickerDialog, pickPaths } from "./FilePickerDialog";
export { OllamaHostField } from "./OllamaHostField";
