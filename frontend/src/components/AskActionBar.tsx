import { useChatData } from '@chainlit/react-client';

/**
 * ask_user_choice の選択肢 — backend では cl.AskActionMessage
 * (WebSocket上の 'ask' + spec.type==='action') として届くため、
 * 汎用のボタン列1つで対応できる。
 */
export function AskActionBar() {
  const { askUser, actions } = useChatData();

  if (!askUser || askUser.spec.type !== 'action') return null;

  const relevantActions = actions.filter(
    (a) => a.forId === askUser.spec.step_id && askUser.spec.keys?.includes(a.id)
  );

  if (relevantActions.length === 0) return null;

  return (
    <div className="ask-action-bar">
      {relevantActions.map((action) => (
        <button
          key={action.id}
          type="button"
          className="ask-action-button"
          onClick={() => askUser.callback(action)}
        >
          {action.label || action.name}
        </button>
      ))}
    </div>
  );
}
