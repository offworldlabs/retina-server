import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    console.error("ErrorBoundary caught:", error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="empty-state error-fallback" role="alert">
          <h2>Something went wrong</h2>
          <p>Please refresh the page. If the problem persists, contact support.</p>
          <div className="error-fallback-actions">
            <button type="button" className="btn btn-primary" onClick={() => this.setState({ hasError: false })}>
              Try again
            </button>
            {/* A lazy page whose chunk failed to load throws the same error on
                every retry, until the document itself is reloaded. */}
            <button type="button" className="btn btn-outline" onClick={() => window.location.reload()}>
              Reload page
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
