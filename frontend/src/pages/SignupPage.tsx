import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { getErrorMessage } from "../api/client";

export default function SignupPage() {
  const { user, signUp, loading } = useAuth();
  const navigate = useNavigate();

  const [form, setForm] = useState({
    firm_name: "",
    full_name: "",
    email: "",
    phone: "",
    password: "",
    firm_gstin: "",
  });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (!loading && user) return <Navigate to="/" replace />;

  function set(field: keyof typeof form, value: string) {
    setForm((f) => ({ ...f, [field]: value }));
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await signUp({
        firm_name: form.firm_name.trim(),
        full_name: form.full_name.trim(),
        email: form.email.trim(),
        password: form.password,
        phone: form.phone || undefined,
        firm_gstin: form.firm_gstin || undefined,
      });
      navigate("/", { replace: true });
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-[80vh] items-center justify-center px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-6 text-center">
          <h1 className="text-xl font-semibold text-slate-900">Create your firm account</h1>
          <p className="mt-1 text-sm text-slate-500">
            You will be the firm owner and can invite colleagues afterwards.
          </p>
        </div>

        <form
          onSubmit={onSubmit}
          className="space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
        >
          {error && (
            <div
              role="alert"
              className="rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700"
            >
              {error}
            </div>
          )}

          <Field
            id="firm_name"
            label="Firm name"
            value={form.firm_name}
            onChange={(v) => set("firm_name", v)}
            required
          />
          <Field
            id="full_name"
            label="Your name"
            value={form.full_name}
            onChange={(v) => set("full_name", v)}
            required
          />
          <Field
            id="email"
            label="Email"
            type="email"
            autoComplete="username"
            value={form.email}
            onChange={(v) => set("email", v)}
            required
          />
          <Field
            id="phone"
            label="Mobile (optional)"
            value={form.phone}
            onChange={(v) => set("phone", v)}
            placeholder="+91…"
          />
          <Field
            id="firm_gstin"
            label="Firm GSTIN (optional)"
            value={form.firm_gstin}
            onChange={(v) => set("firm_gstin", v.toUpperCase())}
            placeholder="15 characters"
          />
          <Field
            id="password"
            label="Password"
            type="password"
            autoComplete="new-password"
            value={form.password}
            onChange={(v) => set("password", v)}
            required
            hint="At least 10 characters."
          />

          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {submitting ? "Creating account…" : "Create account"}
          </button>
        </form>

        <p className="mt-4 text-center text-sm text-slate-600">
          Already have an account?{" "}
          <Link to="/login" className="font-medium text-slate-900 underline">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}

function Field({
  id,
  label,
  value,
  onChange,
  type = "text",
  required = false,
  placeholder,
  hint,
  autoComplete,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  required?: boolean;
  placeholder?: string;
  hint?: string;
  autoComplete?: string;
}) {
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium text-slate-700">
        {label}
      </label>
      <input
        id={id}
        type={type}
        required={required}
        placeholder={placeholder}
        autoComplete={autoComplete}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
      />
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}
