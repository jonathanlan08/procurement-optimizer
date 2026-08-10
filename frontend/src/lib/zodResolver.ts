/** Minimal zod <-> react-hook-form bridge.
 *
 * `@hookform/resolvers` is not in package.json (only `react-hook-form` and
 * `zod` are) and this task's dependency allowlist forbids adding packages —
 * so this reimplements just the handful of lines the official
 * `zodResolver()` provides, using react-hook-form's own public `Resolver`
 * type. Forms here keep zod's output type identical to its input type (no
 * `.transform()`), so the generics stay simple: one `TFieldValues` type
 * parameter, not the two the real package needs to support output-differs-
 * from-input transforms.
 */

import type { FieldErrors, FieldValues, Resolver } from "react-hook-form";
import type { ZodType } from "zod";

export function zodResolver<TFieldValues extends FieldValues>(
  schema: ZodType<TFieldValues>,
): Resolver<TFieldValues> {
  return (values) => {
    const result = schema.safeParse(values);
    if (result.success) {
      return { values: result.data, errors: {} };
    }
    const errors: FieldErrors<TFieldValues> = {};
    const bag = errors as Record<string, { type: string; message: string }>;
    for (const issue of result.error.issues) {
      const path = issue.path.join(".");
      if (!path || path in bag) continue;
      bag[path] = { type: issue.code, message: issue.message };
    }
    return { values: {}, errors };
  };
}
