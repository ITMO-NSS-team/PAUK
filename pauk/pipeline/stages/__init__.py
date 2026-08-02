from .code_links import CodeLinksStage
from .dedup import DedupStage
from .departments import DepartmentsStage
from .pdf import PdfStage
from .persons import PersonsStage
from .repositories import RepositoriesStage

# Dedup runs right after persons: it needs the ORCIDs and name variants
# that stage fetches, and departments should already see merged persons.
ALL_STAGES = (PdfStage, PersonsStage, DedupStage, DepartmentsStage, CodeLinksStage, RepositoriesStage)
