import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** A caught error is dropped when this changes, so a boundary keyed on the
   *  page recovers when the reader moves to another one. A healthy tree is not
   *  remounted by a change. */
  resetKey?: unknown;
  title?: string;
  message?: ReactNode;
}

interface State {
  hasError: boolean;
  resetKey: unknown;
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, resetKey: props.resetKey };
  }

  static getDerivedStateFromError(): Partial<State> {
    return { hasError: true };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    return props.resetKey === state.resetKey ? null : { hasError: false, resetKey: props.resetKey };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    console.error("ErrorBoundary caught:", error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      const {
        title = "Something went wrong",
        message = "Please refresh the page. If the problem persists, contact support.",
      } = this.props;
      return (
        <div className="empty-state error-fallback" role="alert">
          <h2>{title}</h2>
          <p>{message}</p>
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
