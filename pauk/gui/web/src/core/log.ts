/**
 * Logs start, success and failure of one async step, so a stalled fetch is
 * visible in the console. Rethrows the error for the caller to handle.
 */
export async function loggedStep<T>(name: string, action: () => Promise<T>): Promise<T> {
  console.info(`[${name}] loading...`);
  try {
    const result = await action();
    console.info(`[${name}] loaded`);
    return result;
  } catch (error) {
    console.error(`[${name}] failed to load:`, error);
    throw error;
  }
}
