// Top-level safety net. The per-feature FeatureErrorBoundary only wraps the
// active pane; a throw in the shell frame (sidebar, providers, a Rules-of-Hooks
// violation, etc.) would otherwise unmount the whole React tree and leave a
// blank white window. This catches that and shows a recoverable error instead.
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
}

export class RootErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface it for diagnostics (Live log / devtools) instead of swallowing.
    console.error("[errorta] top-level render error:", error, info.componentStack);
  }

  private reload = () => {
    this.setState({ error: null });
    if (typeof window !== "undefined") window.location.reload();
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="root-error">
        <div className="root-error-card">
          <h1>Something went wrong</h1>
          <p>The app hit an unexpected error and couldn’t finish rendering.</p>
          <pre className="root-error-detail">{error.message || String(error)}</pre>
          <button type="button" onClick={this.reload}>
            Reload
          </button>
        </div>
      </div>
    );
  }
}
