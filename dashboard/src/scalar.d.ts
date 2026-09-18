/* What the vendored Scalar bundle (public/vendor/scalar-api-reference-1.69.0)
   defines when it runs. */

interface ScalarInstance {
  destroy(): void;
}

interface Window {
  Scalar?: { createApiReference(el: HTMLElement, config: object): ScalarInstance };
}
