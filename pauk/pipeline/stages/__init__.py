from .author_names import AuthorNamesStage
from .code_links import CodeLinksStage
from .dedup import DedupStage
from .departments import DepartmentsStage
from .emails import EmailsStage
from .github_match import GitHubMatchStage
from .link_relevance import LinkRelevanceStage
from .persons import PersonsStage
from .repo_people import RepoPeopleStage
from .repositories import RepositoriesStage
from .social_graph import SocialGraphStage

# The order is a chain of dependencies. Dedup folds duplicates on what the
# fetching stages brought and rewrites every row naming a merged-away id; it
# does not wait for Russian names, reading the staff catalog itself. Names
# come last, so only canonical persons are named, against every spelling the
# merge collected. link_relevance judges the links code_links produced,
# emails reads the text it downloaded, and github_match needs both those
# addresses and the repositories harvest. social_graph is out of the default
# run: hundreds of API calls that only pay off once github_match has
# confirmed some accounts, so it is run by name and then github_match again.
ALL_STAGES = (
    PersonsStage, DepartmentsStage, CodeLinksStage, LinkRelevanceStage,
    EmailsStage, RepositoriesStage, RepoPeopleStage, DedupStage, GitHubMatchStage,
    AuthorNamesStage,
)
OPTIONAL_STAGES = (SocialGraphStage,)
