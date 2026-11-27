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
