from .author_names import AuthorNamesStage
from .code_links import CodeLinksStage
from .dedup import DedupStage
from .departments import DepartmentsStage
from .emails import EmailsStage
from .github_match import GitHubMatchStage
from .link_relevance import LinkRelevanceStage
from .pdf import PdfStage
from .persons import PersonsStage
from .repositories import RepositoriesStage
from .social_graph import SocialGraphStage

# Dedup runs after the fetching stages: it folds duplicate publications,
# repositories and persons using what they fetched (ORCIDs and name variants,
# GitHub repo ids) and rewrites every prepared row that referenced a
# merged-away id. Russian names come last so only canonical persons are named
# — and so a name is resolved against every spelling the merge collected,
# which is more than the survivor carried on its own. Dedup does not wait for
# that: it reads the staff catalog directly (dedup.staff_identities), since a
# record in it is an identity, and identity is exactly what dedup needs.
# link_relevance runs right after code_links, which is what produces the
# unclassified (is_relevant=None) links it judges. emails reads the full
# text code_links downloaded, and runs before github_match so the addresses
# it finds can identify an account. github_match needs the accounts
# repositories harvests and the authorships dedup has already folded.
# social_graph is not in the default run: it walks outward from confirmed
# accounts, so it only pays off once github_match has confirmed some, and
# each run costs hundreds of API calls. Run it by name, then github_match
# again, until a run finds nothing new.
ALL_STAGES = (
    PdfStage, PersonsStage, DepartmentsStage, CodeLinksStage, LinkRelevanceStage,
    EmailsStage, RepositoriesStage, DedupStage, GitHubMatchStage, AuthorNamesStage,
)
OPTIONAL_STAGES = (SocialGraphStage,)