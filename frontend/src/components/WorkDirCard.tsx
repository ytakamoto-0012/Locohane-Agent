import type { IStep } from '@chainlit/react-client';
import { WORK_DIR_PREFIX } from '../utils/messageTree';
import { isLoopbackHost } from '../utils/serverHost';

export function WorkDirCard({ step }: { step: IStep | undefined }) {
  if (!step || typeof step.output !== 'string') return null;
  const value = step.output.slice(step.output.indexOf(':') + 1).trim();

  return (
    <div className="work-dir-card">
      <div className="work-dir-card-label">
        {WORK_DIR_PREFIX}
        {!isLoopbackHost() && (
          <span className="work-dir-card-scope" title="お使いのPCではなく、Locohane が動いているサーバーPCから見たパスです">
            （サーバーPC上）
          </span>
        )}
      </div>
      <div className="work-dir-card-value">{value}</div>
    </div>
  );
}
