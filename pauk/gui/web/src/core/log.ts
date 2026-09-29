/**
 * Logs start, success and failure of one async step, so a stalled fetch is
 * visible in the console. Rethrows the error for the caller to handle.
 */
export async function loggedStep<T>(name: string, action: () => Promise<T>): Promise<T> {
  console.info(`[${name}] загрузка...`);
  try {
    const result = await action();
    console.info(`[${name}] загружено`);
    return result;
  } catch (error) {
    console.error(`[${name}] ошибка загрузки:`, error);
    throw error;
  }
}
