from rez.backport.total_ordering import total_ordering
import rez.contrib.pyparsing.pyparsing as pp
from rez.exceptions import VersionError
import threading
import copy
import re


re_token = re.compile(r"[a-zA-Z0-9_]+")



@total_ordering
class _Comparable(object):
    def __lt__(self, other):
        raise NotImplementedError

    def __str__(self):
        raise NotImplementedError

    def __ne__(self, other):
        return not (self == other)

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, str(self))



class VersionToken(_Comparable):
    """Token within a version number.

    A version token is that part of a version number that appears between a
    delimiter, typically '.' or '-'. For example, the version number '2.3.07b'
    contains the tokens '2', '3' and '07b' respectively.

    Version tokens are only allowed to contain alphanumerics (any case) and
    underscores.
    """
    @classmethod
    def create_random_token_string(cls):
        """Create a random token string.

        This is used for testing purposes only. The default implementation
        returns a random combination of alphanumerics and underscores.
        """
        chars = \
            [chr(x) for x in range(ord('a'),ord('z')+1)] + \
            [chr(x) for x in range(ord('A'),ord('Z')+1)] + \
            [chr(x) for x in range(ord('0'),ord('9')+1)] + \
            ['_']
        import random
        return ''.join([chars[random.randint(0, len(chars)-1)] for i in range(16)])

    def __init__(self, token):
        """Create a VersionToken.

        Args:
            token: Token string, eg "rc02"
        """
        self.token = token

    def less_than(self, other):
        """Compare to another VersionToken.

        VersionTokens have 'strict weak ordering' - that is, all other
        operators (>, <= etc) are implemented in terms of less-than.

        Args:
            other: The VersionToken object to compare against.

        Returns:
            True if this token is less than other, False otherwise.
        """
        raise NotImplementedError

    def next(self):
        """Returns the next largest token."""
        raise NotImplementedError

    def __lt__(self, other):
        return self.less_than(other)

    def __eq__(self, other):
        return (not self < other) and (not other < self)

    def __str__(self):
        return self.token



class AlphanumericVersionToken(VersionToken):
    """Alphanumeric version token.

    These tokens compare as follows:
    - each token is split into alpha and numeric groups (subtokens);
    - the resulting subtoken list is compared.

    Subtokens compare as follows:
    - alphas come before numbers;
    - alphas are compared alphabetically (_, then A-Z, then a-z);
    - numbers are compared numerically (padding is ignored).

    Some example comparisons that equate to true:
    - "3" < "4"
    - "beta" < "1"
    - "alpha3" < "alpha4"
    - "alpha" < "alpha3"
    - "gamma33" < "33gamma"
    """
    numeric_regex = re.compile("[0-9]+")
    regex = re.compile(r"[a-zA-Z0-9_]+\Z")

    class SubToken(_Comparable):
        def __init__(self, s):
            self.s = s
            self.n = int(s) if s.isdigit() else None

        def __lt__(self, other):
            if self.n is None:
                return (self.s < other.s) if other.n is None else True
            else:
                return False if other.n is None else (self.n < other.n)

        def __eq__(self, other):
            return (self.s == other.s) or \
                (self.n is not None and self.n == other.n)

        def next(self):
            if self.n is None:
                return AlphanumericVersionToken.SubToken(self.s + "_")
            else:
                s = ("%d" % (self.n + 1)).zfill(len(self.s))
                return AlphanumericVersionToken.SubToken(s)

        def __str__(self):
            return self.s

    def __init__(self, token):
        super(AlphanumericVersionToken,self).__init__(token)
        if not self.regex.match(token):
            raise VersionError("Invalid version token: '%s'" % token)
        self.subtokens = self._parse(token)

    def less_than(self, other):
        return (self.subtokens < other.subtokens)

    def copy(self):
        other = copy.copy(self)
        other.subtokens = other.subtokens[:]
        return other

    def next(self):
        other = self.copy()
        tok = other.subtokens.pop()
        other.subtokens.append(tok.next())
        return other

    @classmethod
    def _parse(cls, s):
        subtokens = []
        alphas = cls.numeric_regex.split(s)
        numerics = cls.numeric_regex.findall(s)
        b = True

        while alphas or numerics:
            if b:
                alpha = alphas[0]
                alphas = alphas[1:]
                if alpha:
                    subtokens.append(cls.SubToken(alpha))
            else:
                numeric = numerics[0]
                numerics = numerics[1:]
                subtokens.append(cls.SubToken(numeric))
            b = not b

        return subtokens



class Version(_Comparable):
    """Version object.

    A Version is a sequence of zero or more version tokens, separated by either
    a dot '.' or hyphen '-' delimiters. A Version is constructed with a
    VersionToken class, so that different version schemas can be created. Note
    that separators only affect Version objects cosmetically - in other words,
    the version '1.0.0' is equivalent to '1-0-0'.
    """

    def __init__(self, ver_str='', token_cls=AlphanumericVersionToken):
        """Create a Version object.

        Args:
            ver_str: Version string. The empty string is a special case - this
                is the smallest possible version, and is used to represent
                unversioned objects.
            token_cls: Version token class to use.
        """
        self.ver_str = ver_str
        self.tokens = []
        self.seps = []

        if ver_str:
            toks = re_token.findall(ver_str)
            if not toks:
                raise VersionError(ver_str)

            seps = re_token.split(ver_str)
            if seps[0] or seps[-1] or max(len(x) for x in seps) > 1:
                raise VersionError("Invalid version syntax: '%s'" % ver_str)

            for tok in toks:
                try:
                    self.tokens.append(token_cls(tok))
                except VersionError as e:
                    raise VersionError("Invalid version '%s': %s"
                                       % (ver_str, str(e)))

            self.seps = seps[1:-1]

    def __len__(self):
        return len(self.tokens)

    def copy(self):
        """Return a copy of the version."""
        other = copy.copy(self)
        other.tokens = other.tokens[:]
        other.seps = other.seps[:]
        return other

    def next(self):
        """Return 'next' version. Eg, next(1.2) is 1.3."""
        if self.tokens:
            other = self.copy()
            tok = other.tokens.pop()
            other.tokens.append(tok.next())
            return other
        else:
            raise VersionError("The empty version has no next")

    def __eq__(self, other):
        return (self.tokens == other.tokens)

    def __lt__(self, other):
        return (self.tokens < other.tokens)

    def __str__(self):
        return self.ver_str



class _LowerBound(_Comparable):
    def __init__(self, version, inclusive):
        self.version = version
        self.inclusive = inclusive

    def __str__(self):
        if self.version:
            s = "%s+" if self.inclusive else ">%s"
            return s % self.version
        else:
            return '' if self.inclusive else ">"

    def __eq__(self, other):
        return (self.version == other.version) \
            and (self.inclusive == other.inclusive)

    def __lt__(self, other):
        return (self.version < other.version) \
            or ((self.version == other.version) \
            and (self.inclusive and not other.inclusive))



class _UpperBound(_Comparable):
    def __init__(self, version, inclusive):
        self.version = version
        self.inclusive = inclusive

    def __str__(self):
        s = "<=%s" if self.inclusive else "<%s"
        return s % self.version

    def __eq__(self, other):
        return (self.version == other.version) \
            and (self.inclusive == other.inclusive)

    def __lt__(self, other):
        return (self.version < other.version) \
            or ((self.version == other.version) \
            and (not self.inclusive and other.inclusive))



class _Bound(_Comparable):
    def __init__(self, lower=None, upper=None):
        self.lower = lower
        self.upper = upper

    def __str__(self):
        if self.lower is None:
            return "" if self.upper is None else str(self.upper)
        elif self.upper is None:
            return str(self.lower)
        elif self.lower.version == self.upper.version:
            return "==%s" % str(self.lower.version)
        elif self.lower.inclusive and self.upper.inclusive:
            return "%s..%s" % (self.lower.version, self.upper.version)
        elif (self.lower.inclusive and not self.upper.inclusive) \
            and (self.lower.version.next() == self.upper.version):
            return str(self.lower.version)
        else:
            return "%s%s" % (self.lower, self.upper)

    def __eq__(self, other):
        return (self.lower == other.lower) and (self.upper == other.upper)

    def __lt__(self, other):
        return (self.lower < other.lower) or \
               ((self.lower == other.lower) and (self.upper < other.upper))



class _VersionRangeParser(object):
    parsers = {}

    @classmethod
    def parse(cls, s, debug=False):
        id_ = id(threading.currentThread())
        parser = cls.parsers.get(id_)
        if parser is None:
            parser = _VersionRangeParser()
            cls.parsers[id_] = parser
        return parser._parse(s, debug=debug)

    def __init__(self):
        self.stack = []
        self.bounds = []
        self.debug = False

        # grammar
        token = pp.Word(pp.srange("[0-9a-zA-Z_]"))
        version_sep = pp.oneOf(['.','-'])
        version = pp.Optional(token + pp.ZeroOrMore(version_sep + token)).setParseAction(self._act_version)
        exact_version = ("==" + version).setParseAction(self._act_exact_version)
        inclusive_bound = (version + ".." + version).setParseAction(self._act_inclusive_bound)
        lower_bound = ((pp.oneOf([">", ">="]) + version) | (version + "+")).setParseAction(self._act_lower_bound)
        upper_bound = (pp.oneOf(["<", "<="]) + version).setParseAction(self._act_upper_bound)
        bound = (lower_bound + upper_bound)
        range = (version ^ exact_version ^ lower_bound ^ upper_bound
                 ^ bound ^ inclusive_bound).setParseAction(self._act_range)
        self.ranges = pp.Optional(range + pp.ZeroOrMore("|" + range))

    def action(fn):
        def fn_(self, s, i, tokens):
            fn(self, s, i, tokens)
            if self.debug:
                label = fn.__name__.replace("_act_","")
                print "%-16s%s" % (label+':', s)
                print "%s%s" % ((16+i)*' ', '^'*len(''.join(tokens)))
                print "%s%s" % (16*' ', self.stack)
        return fn_

    @action
    def _act_version(self, s, i, tokens):
        self.stack.append(Version(''.join(tokens)))

    @action
    def _act_exact_version(self, s, i, tokens):
        ver = self.stack.pop()
        lower = _LowerBound(ver, True)
        upper = _UpperBound(ver, True)
        self.stack.append(_Bound(lower, upper))

    @action
    def _act_inclusive_bound(self, s, i, tokens):
        upper_ver = self.stack.pop()
        lower_ver = self.stack.pop()
        self.stack.append(_LowerBound(lower_ver, True))
        self.stack.append(_UpperBound(upper_ver, True))

    @action
    def _act_lower_bound(self, s, i, tokens):
        ver = self.stack.pop()
        exclusive = (">" in list(tokens))
        self.stack.append(_LowerBound(ver, not exclusive))

    @action
    def _act_upper_bound(self, s, i, tokens):
        ver = self.stack.pop()
        exclusive = ("<" in list(tokens))
        self.stack.append(_UpperBound(ver, not exclusive))

    @action
    def _act_range(self, s, i, tokens):
        if len(self.stack) == 1:
            obj = self.stack.pop()
            if isinstance(obj, Version):
                lower = _LowerBound(obj, True)
                upper = _UpperBound(obj.next(), False)
                self.bounds.append(_Bound(lower, upper))
            elif isinstance(obj, _Bound):
                self.bounds.append(obj)
            elif isinstance(obj, _LowerBound):
                self.bounds.append(_Bound(lower=obj))
            else:  # _UpperBound
                self.bounds.append(_Bound(upper=obj))
        else:
            upper = self.stack.pop()
            lower = self.stack.pop()
            self.bounds.append(_Bound(lower, upper))

    def _parse(self, s, debug=False):
        self.stack = []
        self.bounds = []
        self.debug = debug
        self.ranges.parseString(s, parseAll=True)
        return self.bounds



class VersionRange(_Comparable):
    """Version range.

    A version range is a set of zero or more contiguous ranges of versions. For
    example, "3.0 or greater, but less than 4" is a contiguous range that contains
    versions such as "3.0", "3.1.0", "3.99" etc. Version ranges behave something
    like sets - they can be intersected, added and subtracted, but can also be
    inversed. You can test to see if a Version is contained within a VersionRange.

    In the  Rez versioning schema, a VersionRange "3" for example, is the
    superset of any version "3[.X.X...]". The version "3" itself is also within
    this range, and is smaller than "3.0" - any version with common leading
    tokens, but with a larger token count, is the larger version of the two.

    VersionRange objects have a flexible syntax that lets you describe any
    combination of contiguous ranges, including inclusive and exclusive upper
    and lower bounds. This is best explained by example (those listed on the
    same line are equivalent):

    "3": 'superset' syntax, contains "3", "3.0", "3.1" etc;
    "2+", ">=2": inclusive lower bound syntax, contains "2", "2.1", "5.0.0" etc;
    ">2": exclusive lower bound;
    "<5": exclusive upper bound;
    "<=5": inclusive upper bound;
    "1+<5", ">=1<5": inclusive lower, exclusive upper. The most common form of
        a 'closed' version range (ie, one with a lower and upper bound);
    ">1<5": exclusive lower, exclusive upper;
    ">1<=5": exclusive lower, inclusive upper;
    "1+<=5", "1..5": inclusive lower, inclusive upper;

    To describe more than one contiguous range, seperate ranges with the or '|'
    symbol. For example, the version range "4|6+" contains versions such as "4",
    "4.0", "4.3.1", "6", "6.1", "10.0.0", but does not contain any version
    "5[.X.X...X]". If you provide multiple ranges that overlap, they will be
    automatically optimised - for example, the version range "3+<6|4+<8"
    becomes "3+<8".

    Note that the empty string version range represents the superset of all
    possible versions. The empty version can also be used as an upper or lower
    bound, leading to some odd but perfectly valid version range syntax. For
    example, ">" is a valid range - read like ">''", it means "any version
    greater than the empty version".
    """
    def __init__(self, range_str, token_cls=AlphanumericVersionToken,
                 debug_parsing=False):
        """Create a VersionRange object.

        Args:
            range_str: Range string, such as "3", "3+<4.5", "2|6+". The range
                will be optimised, so the string representation of this instance
                may not match range_str. For example, "3+<4" is equivalent to "3".
            token_cls: Version token class to use.
        """
        self.range_str = range_str
        try:
            self.bounds = _VersionRangeParser.parse(range_str,
                                                    debug=debug_parsing)
        except pp.ParseException as e:
            raise VersionError("Syntax error in version range '%s': %s"
                               % (range_str, str(e)))

    def __str__(self):
        return '|'.join(str(x) for x in self.bounds)
