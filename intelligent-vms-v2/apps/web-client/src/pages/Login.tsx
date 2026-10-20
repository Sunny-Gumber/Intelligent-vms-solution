/** Sign-in, including the TOTP second factor. */
import { FormEvent, useState } from "react";
import { api } from "../lib/api";

export function LoginPage({ onSignedIn }: { onSignedIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [needsTotp, setNeedsTotp] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      await api.login(username, password, needsTotp ? totp : undefined);
      onSignedIn();
    } catch (e: any) {
      if (e?.code === "mfa_required") {
        setNeedsTotp(true);
        setError("Enter the six-digit code from your authenticator app.");
      } else if (e?.code === "account_locked") {
        setError(`${e.detail} (retry in ${e.retry_after_s ?? "?"}s)`);
      } else {
        setError(e?.detail ?? "Sign-in failed");
      }
    }
  }

  return (
    <form className="login" onSubmit={submit}>
      <h1>Intelligent VMS</h1>
      <label>Username<input value={username} autoComplete="username"
        onChange={(e) => setUsername(e.target.value)} required /></label>
      <label>Password<input type="password" value={password} autoComplete="current-password"
        onChange={(e) => setPassword(e.target.value)} required /></label>
      {needsTotp && (
        <label>Authenticator code
          <input value={totp} inputMode="numeric" pattern="\d{6}" autoComplete="one-time-code"
            onChange={(e) => setTotp(e.target.value)} required />
        </label>
      )}
      {error && <p role="alert" className="error">{error}</p>}
      <button type="submit">Sign in</button>
    </form>
  );
}
