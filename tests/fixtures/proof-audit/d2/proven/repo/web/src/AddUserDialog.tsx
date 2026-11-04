import { useState } from 'react';

type Mode = 'invite' | 'create';

const CONFIRM_LABEL: Record<Mode, string> = { invite: 'Send invite', create: 'Create account' };

export function AddUserDialog({ email, onConfirm }: { email: string; onConfirm: (mode: Mode) => void }) {
  const [mode, setMode] = useState<Mode>('invite');
  return (
    <div role="dialog" aria-label="Add user">
      <p>No account uses {email}.</p>
      <fieldset>
        <legend>How to add them</legend>
        <label>
          <input type="radio" name="mode" checked={mode === 'invite'} onChange={() => setMode('invite')} />
          Email them an invite
        </label>
        <label>
          <input type="radio" name="mode" checked={mode === 'create'} onChange={() => setMode('create')} />
          Make the account now
        </label>
      </fieldset>
      <p>
        {mode === 'invite'
          ? 'They get an invite email and join when they set a password.'
          : 'The account is created now. They get an email to set their password.'}
      </p>
      <button type="button" className="button-primary" onClick={() => onConfirm(mode)}>
        {CONFIRM_LABEL[mode]}
      </button>
    </div>
  );
}
