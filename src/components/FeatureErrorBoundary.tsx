import { Component, type ReactNode } from "react";

interface FeatureErrorBoundaryProps {
  children: ReactNode;
  featureLabel: string;
  resetKey: string;
}

interface FeatureErrorBoundaryState {
  error: unknown;
  resetKey: string;
}

function describeError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error.trim()) return error;
  return "Unknown render error.";
}

export class FeatureErrorBoundary extends Component<
  FeatureErrorBoundaryProps,
  FeatureErrorBoundaryState
> {
  state: FeatureErrorBoundaryState = {
    error: null,
    resetKey: this.props.resetKey,
  };

  static getDerivedStateFromError(error: unknown): Partial<FeatureErrorBoundaryState> {
    return { error };
  }

  static getDerivedStateFromProps(
    props: FeatureErrorBoundaryProps,
    state: FeatureErrorBoundaryState,
  ): Partial<FeatureErrorBoundaryState> | null {
    if (props.resetKey !== state.resetKey) {
      return { error: null, resetKey: props.resetKey };
    }
    return null;
  }

  private retry = () => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error === null) return this.props.children;

    return (
      <section className="feature-pane feature-pane-error" role="alert">
        <header className="feature-pane-header">
          <h1>{this.props.featureLabel} failed to load</h1>
          <p className="feature-pane-spec">
            This tab hit a render error. The rest of Errorta is still running.
          </p>
        </header>
        <p className="feature-pane-error-details">
          {describeError(this.state.error)}
        </p>
        <button type="button" onClick={this.retry}>
          Retry
        </button>
      </section>
    );
  }
}

export default FeatureErrorBoundary;
