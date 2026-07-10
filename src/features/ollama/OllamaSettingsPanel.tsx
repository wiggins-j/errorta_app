import CustomHostInput from "./CustomHostInput";
import StoragePathInput from "./StoragePathInput";
import type { OllamaSettings } from "./types";

interface Props {
  settings: OllamaSettings | null;
  onUpdateHost: (host: string) => Promise<void>;
  onUpdateStorage: (path: string | null) => Promise<void>;
}

export default function OllamaSettingsPanel({ settings, onUpdateHost, onUpdateStorage }: Props) {
  return (
    <div className="feature-pane-card" style={{ marginTop: 16 }}>
      <h2 style={{ marginTop: 0 }}>Settings</h2>
      <CustomHostInput
        value={settings?.host ?? "http://localhost:11434"}
        onSave={onUpdateHost}
        disabled={!settings}
      />
      <StoragePathInput
        value={settings?.storage_path ?? null}
        onSave={onUpdateStorage}
        disabled={!settings}
      />
      <dl style={{ marginTop: 16, fontSize: 13 }}>
        <dt>Installed version</dt>
        <dd>{settings?.installed_version ?? "—"}</dd>
        <dt>Last install</dt>
        <dd>{settings?.last_install_at ?? "—"}</dd>
        <dt>Managed by Errorta</dt>
        <dd>{settings?.managed_by_errorta ? "yes" : "no"}</dd>
      </dl>
    </div>
  );
}
