// Синтетические данные для тестов — единственное место в репозитории, где
// они вообще есть. Раньше жили в src/core/ и грузились через loadSample*(),
// которые ничем не отличались от настоящих loadGraphData()/loadDetails()
// (core/data.ts) кроме источника — так было нужно на этапе v2-прототипа,
// когда реальных данных ещё не было вообще. Теперь генератор реально
// работает (new_generate), src/core/data.ts только про настоящие данные, а
// этот файл — про тестовые, и живёт рядом с тестами, а не в поставляемом
// исходнике приложения.
//
// Значения — синтетические, не срез настоящего снепшота: там реальные
// email/ORCID/аффилиации живых людей, которым не место в git-коммите (см.
// contracts/graph.ts на актуальную форму полей — фикстуры обязаны за ней
// поспевать).

import type { AuthorDetail, GraphData, PubDetail, RepoDetail } from "../src/contracts/graph";
import { assertGraphData } from "../src/core/data";
import sampleGraphData from "./fixtures/graph-data.sample.json";
import sampleAuthorDetails from "./fixtures/authors-detail.sample.json";
import sampleRepoDetails from "./fixtures/repos-detail.sample.json";
import samplePubDetails from "./fixtures/pubs-detail.sample.json";

/**
 * Небольшой, но полный набор (департаменты, авторы, репозитории,
 * публикации, все виды рёбер), соответствующий контракту {@link GraphData}.
 *
 * @returns Промис с фикстур-данными (асинхронность — только ради единого
 *   интерфейса с настоящим `loadGraphData()`, сам импорт JSON синхронный).
 */
export async function loadSampleGraphData(): Promise<GraphData> {
  const data = sampleGraphData as GraphData;
  assertGraphData(data);
  return data;
}

/** @returns Промис со списком тестовых деталей публикаций. */
export async function loadSamplePubDetails(): Promise<PubDetail[]> {
  return samplePubDetails as PubDetail[];
}

/** @returns Промис со списком тестовых деталей авторов. */
export async function loadSampleAuthorDetails(): Promise<AuthorDetail[]> {
  return sampleAuthorDetails as AuthorDetail[];
}

/** @returns Промис со списком тестовых деталей репозиториев. */
export async function loadSampleRepoDetails(): Promise<RepoDetail[]> {
  return sampleRepoDetails as RepoDetail[];
}
