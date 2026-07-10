// F005 — "Watch a folder" entry point. Opens the native folder picker via
// @tauri-apps/plugin-dialog and hands the chosen path to the dialog.

interface Props {
  onPick: (path: string) => void;
  disabled?: boolean;
  label?: string;
}

export function FolderWatchButton({ onPick, disabled, label }: Props) {
  async function handleClick() {
    try {
      const dialog = await import("@tauri-apps/plugin-dialog");
      const picked = await dialog.open({
        directory: true,
        multiple: false,
        title: "Select a folder to watch",
      });
      if (typeof picked === "string" && picked) {
        onPick(picked);
      }
    } catch {
      // Fallback for non-Tauri / browser dev: prompt for a path.
      const typed = window.prompt("Folder path to watch");
      if (typed) onPick(typed);
    }
  }

  return (
    <button type="button" onClick={handleClick} disabled={disabled}>
      {label ?? "Watch a folder"}
    </button>
  );
}
