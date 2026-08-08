import type {
  TeachingReview,
  TeachingReviewDetail,
} from "@/lib/teaching-api";

export function reviewEvidenceMatches(
  review: TeachingReview,
  detail: TeachingReviewDetail | null,
): boolean {
  if (!detail) return false;
  const evidence = detail.review;
  return (
    evidence.id === review.id &&
    evidence.assetId === review.assetId &&
    evidence.draftId === review.draftId &&
    evidence.draftRevision === review.draftRevision &&
    evidence.documentSha256 === review.documentSha256 &&
    evidence.validationReportSha256 === review.validationReportSha256
  );
}

export function canDecideTeachingReview(input: {
  review: TeachingReview | null | undefined;
  detail: TeachingReviewDetail | null;
  currentUserId: string | null;
  loading: boolean;
  error: string | null;
}): boolean {
  const { review, detail, currentUserId, loading, error } = input;
  return Boolean(
    review &&
      review.status === "pending" &&
      currentUserId &&
      review.submittedBy !== currentUserId &&
      !loading &&
      !error &&
      reviewEvidenceMatches(review, detail),
  );
}

export interface TeachingAttemptRegistry {
  keyFor(fingerprint: string): string;
  settle(fingerprint: string): void;
}

function attemptKey(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  return `${prefix}-${suffix}`;
}

export function createTeachingAttemptRegistry(
  createKey: (fingerprint: string) => string = () =>
    attemptKey("teaching-attempt"),
): TeachingAttemptRegistry {
  const attempts = new Map<string, string>();
  return {
    keyFor(fingerprint) {
      const existing = attempts.get(fingerprint);
      if (existing) return existing;
      const created = createKey(fingerprint);
      attempts.set(fingerprint, created);
      return created;
    },
    settle(fingerprint) {
      attempts.delete(fingerprint);
    },
  };
}

export function createTeachingPublicationAttemptRegistry(
  createKey: (assetId: string) => string = () =>
    attemptKey("tenant-publication"),
): TeachingAttemptRegistry {
  return createTeachingAttemptRegistry(createKey);
}

export function shouldApplyOutlineResponse(input: {
  requestText: string;
  currentText: string;
  requestEpoch: number;
  currentEpoch: number;
}): boolean {
  return (
    input.requestText === input.currentText &&
    input.requestEpoch === input.currentEpoch
  );
}

export function isTeachingClassroomEditable(lifecycleState: string): boolean {
  return lifecycleState === "editing";
}

export interface TeachingOperationSnapshot {
  assetId: string;
  draftId: string;
  revision: string;
  epoch: number;
}

export function isCurrentTeachingOperation(
  expected: TeachingOperationSnapshot,
  current: TeachingOperationSnapshot,
): boolean {
  return (
    expected.assetId === current.assetId &&
    expected.draftId === current.draftId &&
    expected.revision === current.revision &&
    expected.epoch === current.epoch
  );
}
