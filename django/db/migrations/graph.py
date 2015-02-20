from __future__ import unicode_literals

from collections import deque

from django.db.migrations.state import ProjectState
from django.utils.datastructures import OrderedSet
from django.utils.encoding import python_2_unicode_compatible


@python_2_unicode_compatible
class Node(object):
    __slots__ = ['key', 'app_label', 'migration_name', 'migration', 'children', 'parents', 'descendants', 'ancestors']

    def __init__(self, key, migration):
        self.key = key
        self.app_label = key[0]
        self.migration_name = key[1]
        self.migration = migration
        self.children = set()
        self.parents = set()

    def __eq__(self, other):
        return (isinstance(other, Node) and self.app_label == other.app_label and self.migration_name == other.migration_name
                or isinstance(other, tuple) and self.app_label == other[0] and self.migration_name == other[1])

    def __hash__(self):
        return hash((self.app_label, self.migration_name))

    def __getitem__(self, item):
        return self.key[item]

    def add_parent(self, parent):
        self.parents.add(parent)

    def add_child(self, child):
        self.children.add(child)

    def get_descendants(self):
        if not hasattr(self, 'descendants'):
            descendants = deque([self])
            for child in sorted(self.children):
                descendants.extendleft(reversed(child.get_descendants()))
            self.descendants = list(OrderedSet(descendants))
        return self.descendants

    def get_ancestors(self):
        if not hasattr(self, 'ancestors'):
            ancestors = deque([self])
            for parent in sorted(self.parents):
                ancestors.extendleft(reversed(parent.get_ancestors()))
            self.ancestors = list(OrderedSet(ancestors))
        return self.ancestors

    def __str__(self):
        return str((self.app_label, self.migration_name))

    def __repr__(self):
        return '<Node: (%s, %s)>' % (self.app_label, self.migration_name)


@python_2_unicode_compatible
class MigrationGraph(object):
    """
    Represents the digraph of all migrations in a project.

    Each migration is a node, and each dependency is an edge. There are
    no implicit dependencies between numbered migrations - the numbering is
    merely a convention to aid file listing. Every new numbered migration
    has a declared dependency to the previous number, meaning that VCS
    branch merges can be detected and resolved.

    Migrations files can be marked as replacing another set of migrations -
    this is to support the "squash" feature. The graph handler isn't responsible
    for these; instead, the code to load them in here should examine the
    migration files and if the replaced migrations are all either unapplied
    or not present, it should ignore the replaced ones, load in just the
    replacing migration, and repoint any dependencies that pointed to the
    replaced migrations to point to the replacing one.

    A node should be a tuple: (app_path, migration_name). The tree special-cases
    things within an app - namely, root nodes and leaf nodes ignore dependencies
    to other apps.
    """

    def __init__(self):
        self.node_map = {}
        self.nodes = {}
        self.cached = False

    def add_node(self, key, implementation):
        node = Node(key, implementation)
        self.node_map[key] = node
        self.nodes[node] = implementation
        self.clear_cache()

    def add_dependency(self, migration, child, parent):
        if child not in self.node_map:
            raise NodeNotFoundError(
                "Migration %s dependencies reference nonexistent child node %r" % (migration, child),
                child
            )
        if parent not in self.node_map:
            raise NodeNotFoundError(
                "Migration %s dependencies reference nonexistent parent node %r" % (migration, parent),
                parent
            )
        self.node_map[child].add_parent(self.node_map[parent])
        self.node_map[parent].add_child(self.node_map[child])
        self.clear_cache()

    def clear_cache(self):
        if self.cached:
            for node in self.nodes:
                if hasattr(node, 'ancestors'):
                    del node.ancestors
                if hasattr(node, 'descendants'):
                    del node.descendants
            self.cached = False

    def forwards_plan(self, node):
        """
        Given a node, returns a list of which previous nodes (dependencies)
        must be applied, ending with the node itself.
        This is the list you would follow if applying the migrations to
        a database.
        """
        if node not in self.nodes:
            raise NodeNotFoundError("Node %r not a valid node" % (node, ), node)
        self.ensure_not_cyclic(node, lambda x: self.node_map[x].parents)
        self.cached = True
        return self.node_map[node].get_ancestors()

    def backwards_plan(self, node):
        """
        Given a node, returns a list of which dependent nodes (dependencies)
        must be unapplied, ending with the node itself.
        This is the list you would follow if removing the migrations from
        a database.
        """
        if node not in self.nodes:
            raise NodeNotFoundError("Node %r not a valid node" % (node, ), node)
        self.ensure_not_cyclic(node, lambda x: self.node_map[x].children)
        self.cached = True
        return self.node_map[node].get_descendants()

    def root_nodes(self, app=None):
        """
        Returns all root nodes - that is, nodes with no dependencies inside
        their app. These are the starting point for an app.
        """
        roots = set()
        for node in self.nodes:
            if (not any(key.app_label == node.app_label for key in list(node.parents))
                    and (not app or app == node.app_label)):
                roots.add(node)
        return sorted(roots)

    def leaf_nodes(self, app=None):
        """
        Returns all leaf nodes - that is, nodes with no dependents in their app.
        These are the "most current" version of an app's schema.
        Having more than one per app is technically an error, but one that
        gets handled further up, in the interactive command - it's usually the
        result of a VCS merge and needs some user input.
        """
        leaves = set()
        for node in self.nodes:
            if (not any(key.app_label == node.app_label for key in list(node.children))
                    and (not app or app == node.app_label)):
                leaves.add(node)
        return sorted(leaves)

    def ensure_not_cyclic(self, start, get_children):
        # Algo from GvR:
        # http://neopythonic.blogspot.co.uk/2009/01/detecting-cycles-in-directed-graph.html
        todo = set(self.nodes.keys())
        while todo:
            node = todo.pop()
            stack = [node]
            while stack:
                top = stack[-1]
                for node in get_children(top):
                    if node in stack:
                        cycle = stack[stack.index(node):]
                        raise CircularDependencyError(", ".join("%s.%s" % n for n in cycle))
                    if node in todo:
                        stack.append(node)
                        todo.remove(node)
                        break
                else:
                    node = stack.pop()

    def dfs(self, start, get_children):
        """
        Iterative depth first search, for finding dependencies.
        """
        self.ensure_not_cyclic(start, get_children)
        visited = deque()
        visited.append(start)
        stack = deque(sorted(get_children(start)))
        while stack:
            node = stack.popleft()
            visited.appendleft(node)
            children = sorted(get_children(node), reverse=True)
            # reverse sorting is needed because prepending using deque.extendleft
            # also effectively reverses values
            stack.extendleft(children)

        return list(OrderedSet(visited))

    def __str__(self):
        return "Graph: %s nodes, %s edges" % (
            len(self.nodes),
            sum(len(node.parents) for node in self.nodes),
        )

    def make_state(self, nodes=None, at_end=True, real_apps=None):
        """
        Given a migration node or nodes, returns a complete ProjectState for it.
        If at_end is False, returns the state before the migration has run.
        If nodes is not provided, returns the overall most current project state.
        """
        if nodes is None:
            nodes = list(self.leaf_nodes())
        if len(nodes) == 0:
            return ProjectState()
        if not isinstance(nodes[0], (tuple, Node)):
            nodes = [nodes]
        plan = []
        for node in nodes:
            for migration in self.forwards_plan(node):
                if migration not in plan:
                    if not at_end and migration in nodes:
                        continue
                    plan.append(migration)
        project_state = ProjectState(real_apps=real_apps)
        for node in plan:
            project_state = self.nodes[node].mutate_state(project_state)
        return project_state

    def __contains__(self, node):
        return node in self.nodes


class CircularDependencyError(Exception):
    """
    Raised when there's an impossible-to-resolve circular dependency.
    """
    pass


@python_2_unicode_compatible
class NodeNotFoundError(LookupError):
    """
    Raised when an attempt on a node is made that is not available in the graph.
    """

    def __init__(self, message, node):
        self.message = message
        self.node = node

    def __str__(self):
        return self.message

    def __repr__(self):
        return "NodeNotFoundError(%r)" % self.node
