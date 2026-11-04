import { useState } from 'react';

type Mode = 'invite' | 'create';

const CONFIRM_LABEL: Record<Mode, string> = { invite: 'Send invite', create: 'Create account' };

export function AddUserDialog({ email, onConfirm }: { email: string; onConfirm: (mode: Mode) => void }) {
  const [mode, setMode] = useState<Mode>('invite');
  return (
    <div role="dialog" aria-label="Add user">
      <p>No account uses {email}.</p>
      <div role="group" aria-label="How to add them">
        {(
          [
            ['invite', 'Send invite'],
            ['create', 'Create account now'],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            type="button"
            aria-pressed={mode === value}
            className={mode === value ? 'button-primary' : 'button-plain'}
            onClick={() => setMode(value)}
          >
            {label}
          </button>
        ))}
      </div>
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
