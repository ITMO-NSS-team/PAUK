from .code_links import CodeLinksStage
from .dedup import DedupStage
from .departments import DepartmentsStage
from .link_relevance import LinkRelevanceStage
from .pdf import PdfStage
from .persons import PersonsStage
from .repositories import RepositoriesStage
from .russian_names import RussianNamesStage

# Dedup runs after the fetching stages: it folds duplicate publications,
# repositories and persons using what they fetched (ORCIDs and name variants,
# GitHub repo ids) and rewrites every prepared row that referenced a
# merged-away id. Russian names come last so only canonical persons are named
# — and so a name is resolved against every spelling the merge collected,
# which is more than the survivor carried on its own. Dedup does not wait for
# that: it reads the staff catalog directly (dedup.staff_identities), since a
# record in it is an identity, and identity is exactly what dedup needs.
# link_relevance runs right after code_links, which is what produces the
# unclassified (is_relevant=None) links it judges.
ALL_STAGES = (
    PdfStage, PersonsStage, DepartmentsStage, CodeLinksStage, LinkRelevanceStage,
    RepositoriesStage, DedupStage, RussianNamesStage,
)