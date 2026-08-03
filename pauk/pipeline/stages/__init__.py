from .code_links import CodeLinksStage
from .dedup import DedupStage
from .departments import DepartmentsStage
from .pdf import PdfStage
from .persons import PersonsStage
from .repositories import RepositoriesStage
from .russian_names import RussianNamesStage

# Dedup runs after the fetching stages: it folds duplicate publications,
# repositories and persons using what they fetched (ORCIDs and name variants,
# GitHub repo ids) and rewrites every prepared row that referenced a
# merged-away id. Russian names come last so only canonical persons are named.
ALL_STAGES = (PdfStage, PersonsStage, DepartmentsStage, CodeLinksStage,
              RepositoriesStage, DedupStage, RussianNamesStage)
