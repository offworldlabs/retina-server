// Compiled into every workspace whose tsconfig extends tsconfig.strict.json,
// and imported and run by nothing. Each line under @ts-expect-error has to stay
// a type error, so tsc fails there the day the setting it names stops applying.

declare const maybe: string | null;

// strictNullChecks
// @ts-expect-error a value that may be null has no method to call
maybe.toString();

// noImplicitAny
// @ts-expect-error a parameter with no type is an implicit any
export const untyped = (value) => value;

// strict, through useUnknownInCatchVariables
try {
  maybe?.toString();
} catch (error) {
  // @ts-expect-error a caught value is unknown
  error.toString();
}
