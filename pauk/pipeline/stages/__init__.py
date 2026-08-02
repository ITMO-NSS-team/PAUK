from .code_links import CodeLinksStage
from .dedup import DedupStage
from .departments import DepartmentsStage
from .pdf import PdfStage
from .persons import PersonsStage
from .repositories import RepositoriesStage

# Dedup runs last: it folds duplicate publications, repositories and persons
# using what the other stages fetched (ORCIDs and name variants, GitHub repo
# ids) and rewrites every prepared row that referenced a merged-away id.
ALL_STAGES = (PdfStage, PersonsStage, DepartmentsStage, CodeLinksStage, RepositoriesStage, DedupStage)
