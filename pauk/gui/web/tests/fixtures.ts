// Synthetic data, not a real snapshot slice: the real one holds personal
// data. Keep in sync with contracts/graph.ts.

import type { AuthorDetail, GraphData, PubDetail, RepoDetail } from "../src/contracts/graph";
import { assertGraphData } from "../src/core/data";
import sampleGraphData from "./fixtures/graph-data.sample.json";
import sampleAuthorDetails from "./fixtures/authors-detail.sample.json";
import sampleRepoDetails from "./fixtures/repos-detail.sample.json";
import samplePubDetails from "./fixtures/pubs-detail.sample.json";

export async function loadSampleGraphData(): Promise<GraphData> {
  const data = sampleGraphData as GraphData;
  assertGraphData(data);
  return data;
}

export async function loadSamplePubDetails(): Promise<PubDetail[]> {
  return samplePubDetails as PubDetail[];
}

export async function loadSampleAuthorDetails(): Promise<AuthorDetail[]> {
  return sampleAuthorDetails as AuthorDetail[];
}

export async function loadSampleRepoDetails(): Promise<RepoDetail[]> {
  return sampleRepoDetails as RepoDetail[];
}
