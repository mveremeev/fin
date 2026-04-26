inputFile = "example.fin"

import datetime
import re

from toml.tz import TomlTz


TIME_RE = re.compile(r"([0-9]{2}):([0-9]{2}):([0-9]{2})(\.([0-9]{3,6}))?")
_numberWithUnderscores = re.compile(r"([0-9])(_([0-9]))*")
_groupNameRe = re.compile(r"^[A-Za-z0-9_-]+$")


class FinDecodeError(ValueError):
    """Base FIN exception / error."""

    def __init__(self, msg, doc, pos):
        lineNo = doc.count("\n", 0, pos) + 1
        colNo = pos - doc.rfind("\n", 0, pos)
        errorMessage = "{} (line {} column {} char {})".format(
            msg, lineNo, colNo, pos
        )
        super().__init__(errorMessage)
        self.msg = msg
        self.doc = doc
        self.pos = pos
        self.lineno = lineNo
        self.colno = colNo


def _strictlyValidNum(n):
    n = n.strip()
    if not n:
        return False
    if n[0] == "_":
        return False
    if n[-1] == "_":
        return False
    if "_." in n or "._" in n:
        return False
    if len(n) == 1:
        return True
    if n[0] == "0" and n[1] not in [".", "o", "b", "x"]:
        return False
    if n[0] == "+" or n[0] == "-":
        n = n[1:]
        if len(n) > 1 and n[0] == "0" and n[1] != ".":
            return False
    if "__" in n:
        return False
    return True


def _loadDate(val):
    microsecond = 0
    tz = None
    try:
        if len(val) > 19:
            if val[19] == ".":
                if val[-1].upper() == "Z":
                    subsecondval = val[20:-1]
                    tzval = "Z"
                else:
                    subsecondvalandtz = val[20:]
                    if "+" in subsecondvalandtz:
                        splitpoint = subsecondvalandtz.index("+")
                        subsecondval = subsecondvalandtz[:splitpoint]
                        tzval = subsecondvalandtz[splitpoint:]
                    elif "-" in subsecondvalandtz:
                        splitpoint = subsecondvalandtz.index("-")
                        subsecondval = subsecondvalandtz[:splitpoint]
                        tzval = subsecondvalandtz[splitpoint:]
                    else:
                        tzval = None
                        subsecondval = subsecondvalandtz
                if tzval is not None:
                    tz = TomlTz(tzval)
                microsecond = int(int(subsecondval) * (10 ** (6 - len(subsecondval))))
            else:
                tz = TomlTz(val[19:].upper())
    except ValueError:
        tz = None
    if "-" not in val[1:]:
        return None
    try:
        if len(val) == 10:
            d = datetime.date(int(val[:4]), int(val[5:7]), int(val[8:10]))
        else:
            d = datetime.datetime(
                int(val[:4]),
                int(val[5:7]),
                int(val[8:10]),
                int(val[11:13]),
                int(val[14:16]),
                int(val[17:19]),
                microsecond,
                tz,
            )
    except ValueError:
        return None
    return d


def _loadUnicodeEscapes(v, hexbytes, prefix):
    skip = False
    i = len(v) - 1
    while i > -1 and v[i] == "\\":
        skip = not skip
        i -= 1
    for hx in hexbytes:
        if skip:
            skip = False
            i = len(hx) - 1
            while i > -1 and hx[i] == "\\":
                skip = not skip
                i -= 1
            v += prefix
            v += hx
            continue
        hxb = ""
        i = 0
        hxblen = 4
        if prefix == "\\U":
            hxblen = 8
        hxb = "".join(hx[i : i + hxblen]).lower()
        if hxb.strip("0123456789abcdef"):
            raise ValueError("Invalid escape sequence: " + hxb)
        if hxb[0] == "d" and hxb[1].strip("01234567"):
            raise ValueError(
                "Invalid escape sequence: "
                + hxb
                + ". Only scalar unicode points are allowed."
            )
        v += chr(int(hxb, 16))
        v += str(hx[len(hxb) :])
    return v


_escapes = ["0", "b", "f", "n", "r", "t", '"']
_escapedchars = ["\0", "\b", "\f", "\n", "\r", "\t", '"']
_escapeToEscapedChars = dict(zip(_escapes, _escapedchars))


def _unescape(v):
    """Unescape characters in a TOML string."""
    i = 0
    backslash = False
    while i < len(v):
        if backslash:
            backslash = False
            if v[i] in _escapes:
                v = v[: i - 1] + _escapeToEscapedChars[v[i]] + v[i + 1 :]
            elif v[i] == "\\":
                v = v[: i - 1] + v[i:]
            elif v[i] == "u" or v[i] == "U":
                i += 1
            else:
                raise ValueError("Reserved escape sequence used")
            continue
        elif v[i] == "\\":
            backslash = True
        i += 1
    return v


class InlineTableDict:
    """Sentinel subclass of dict for inline tables."""


class FinDecoder:
    def __init__(self, _dict=dict):
        self._dict = _dict

    def getEmptyTable(self):
        return self._dict()

    def getEmptyInlineTable(self):
        class DynamicInlineTableDict(self._dict, InlineTableDict):
            """Concrete sentinel subclass for inline tables."""

        return DynamicInlineTableDict()

    def loadInlineObject(self, line, currentlevel, multikey=False, multibackslash=False):
        candidate_groups = line[1:-1].split(",")
        groups = []
        if len(candidate_groups) == 1 and not candidate_groups[0].strip():
            candidate_groups.pop()
        while len(candidate_groups) > 0:
            candidate_group = candidate_groups.pop(0)
            try:
                _, value = candidate_group.split("=", 1)
            except ValueError:
                raise ValueError("Invalid inline table encountered")
            value = value.strip()
            if (
                (value[0] == value[-1] and value[0] in ('"', "'"))
                or (value[0] in "-0123456789" or value in ("true", "false"))
                or (value[0] == "[" and value[-1] == "]")
                or (value[0] == "{" and value[-1] == "}")
            ):
                groups.append(candidate_group)
            elif len(candidate_groups) > 0:
                candidate_groups[0] = candidate_group + "," + candidate_groups[0]
            else:
                raise ValueError("Invalid inline table value encountered")
        for group in groups:
            status = self.loadLine(group, currentlevel, multikey, multibackslash)
            if status is not None:
                break

    def _getSplitOnQuotes(self, line):
        doublequotesplits = line.split('"')
        quoted = False
        quotesplits = []
        if len(doublequotesplits) > 1 and "'" in doublequotesplits[0]:
            singlequotesplits = doublequotesplits[0].split("'")
            doublequotesplits = doublequotesplits[1:]
            while len(singlequotesplits) % 2 == 0 and len(doublequotesplits):
                singlequotesplits[-1] += '"' + doublequotesplits[0]
                doublequotesplits = doublequotesplits[1:]
                if "'" in singlequotesplits[-1]:
                    singlequotesplits = (
                        singlequotesplits[:-1] + singlequotesplits[-1].split("'")
                    )
            quotesplits += singlequotesplits
        for doublequotesplit in doublequotesplits:
            if quoted:
                quotesplits.append(doublequotesplit)
            else:
                quotesplits += doublequotesplit.split("'")
                quoted = not quoted
        return quotesplits

    def loadLine(self, line, currentlevel, multikey, multibackslash):
        i = 1
        quotesplits = self._getSplitOnQuotes(line)
        quoted = False
        for quotesplit in quotesplits:
            if not quoted and "=" in quotesplit:
                break
            i += quotesplit.count("=")
            quoted = not quoted
        pair = line.split("=", i)
        strictly_valid = _strictlyValidNum(pair[-1])
        if _numberWithUnderscores.match(pair[-1]):
            pair[-1] = pair[-1].replace("_", "")
        while (
            len(pair[-1])
            and pair[-1][0] != " "
            and pair[-1][0] != "\t"
            and pair[-1][0] != "'"
            and pair[-1][0] != '"'
            and pair[-1][0] != "["
            and pair[-1][0] != "{"
            and pair[-1].strip() != "true"
            and pair[-1].strip() != "false"
        ):
            try:
                float(pair[-1])
                break
            except ValueError:
                pass
            if _loadDate(pair[-1]) is not None:
                break
            if TIME_RE.match(pair[-1]):
                break
            i += 1
            prev_val = pair[-1]
            pair = line.split("=", i)
            if prev_val == pair[-1]:
                raise ValueError("Invalid date or number")
            if strictly_valid:
                strictly_valid = _strictlyValidNum(pair[-1])
        pair = ["=".join(pair[:-1]).strip(), pair[-1].strip()]
        if "." in pair[0]:
            if '"' in pair[0] or "'" in pair[0]:
                quotesplits = self._getSplitOnQuotes(pair[0])
                quoted = False
                levels = []
                for quotesplit in quotesplits:
                    if quoted:
                        levels.append(quotesplit)
                    else:
                        levels += [level.strip() for level in quotesplit.split(".")]
                    quoted = not quoted
            else:
                levels = pair[0].split(".")
            while levels[-1] == "":
                levels = levels[:-1]
            for level in levels[:-1]:
                if level == "":
                    continue
                if level not in currentlevel:
                    currentlevel[level] = self.getEmptyTable()
                currentlevel = currentlevel[level]
            pair[0] = levels[-1].strip()
        elif (pair[0][0] == '"' or pair[0][0] == "'") and (pair[0][-1] == pair[0][0]):
            pair[0] = _unescape(pair[0][1:-1])
        k, koffset = self._loadLineMultilineStr(pair[1])
        if k > -1:
            while k > -1 and pair[1][k + koffset] == "\\":
                multibackslash = not multibackslash
                k -= 1
            if multibackslash:
                multilinestr = pair[1][:-1]
            else:
                multilinestr = pair[1] + "\n"
            multikey = pair[0]
        else:
            value, vtype = self.loadValue(pair[1], strictly_valid)
        try:
            currentlevel[pair[0]]
            raise ValueError("Duplicate keys!")
        except TypeError:
            raise ValueError("Duplicate keys!")
        except KeyError:
            if multikey:
                return multikey, multilinestr, multibackslash
            else:
                currentlevel[pair[0]] = value

    def _loadLineMultilineStr(self, p):
        poffset = 0
        if len(p) < 3:
            return -1, poffset
        if p[0] == "[" and (p.strip()[-1] != "]" and self._loadArrayIsStrArray(p)):
            newp = p[1:].strip().split(",")
            while len(newp) > 1 and newp[-1][0] != '"' and newp[-1][0] != "'":
                newp = newp[:-2] + [newp[-2] + "," + newp[-1]]
            newp = newp[-1]
            poffset = len(p) - len(newp)
            p = newp
        if p[0] != '"' and p[0] != "'":
            return -1, poffset
        if p[1] != p[0] or p[2] != p[0]:
            return -1, poffset
        if len(p) > 5 and p[-1] == p[0] and p[-2] == p[0] and p[-3] == p[0]:
            return -1, poffset
        return len(p) - 1, poffset

    def loadValue(self, v, strictlyValid=True):
        if not v:
            raise ValueError("Empty value is invalid")
        if v == "true":
            return (True, "bool")
        elif v.lower() == "true":
            raise ValueError("Only all lowercase booleans allowed")
        elif v == "false":
            return (False, "bool")
        elif v.lower() == "false":
            raise ValueError("Only all lowercase booleans allowed")
        elif v[0] == '"' or v[0] == "'":
            quotechar = v[0]
            testv = v[1:].split(quotechar)
            triplequote = False
            triplequotecount = 0
            if len(testv) > 1 and testv[0] == "" and testv[1] == "":
                testv = testv[2:]
                triplequote = True
            closed = False
            for tv in testv:
                if tv == "":
                    if triplequote:
                        triplequotecount += 1
                    else:
                        closed = True
                else:
                    oddbackslash = False
                    try:
                        i = -1
                        j = tv[i]
                        while j == "\\":
                            oddbackslash = not oddbackslash
                            i -= 1
                            j = tv[i]
                    except IndexError:
                        pass
                    if not oddbackslash:
                        if closed:
                            raise ValueError("Found tokens after a closed " + "string. Invalid TOML.")
                        else:
                            if not triplequote or triplequotecount > 1:
                                closed = True
                            else:
                                triplequotecount = 0
            if quotechar == '"':
                if triplequote:
                    v = re.sub(r"\\[ \t]*\n[ \t]*", "", v)
                escapeseqs = v.split("\\")[1:]
                backslash = False
                for i in escapeseqs:
                    if i == "":
                        backslash = not backslash
                    else:
                        if i[0] not in _escapes and (i[0] != "u" and i[0] != "U" and not backslash):
                            raise ValueError("Reserved escape sequence used")
                        if backslash:
                            backslash = False
                for prefix in ["\\u", "\\U"]:
                    if prefix in v:
                        hexbytes = v.split(prefix)
                        v = _loadUnicodeEscapes(hexbytes[0], hexbytes[1:], prefix)
                v = _unescape(v)
            if len(v) > 1 and v[1] == quotechar and (len(v) < 3 or v[1] == v[2]):
                v = v[2:-2]
            return (v[1:-1], "str")
        elif v[0] == "[":
            return (self.loadArray(v), "array")
        elif v[0] == "{":
            inline_object = self.getEmptyInlineTable()
            self.loadInlineObject(v, inline_object)
            return (inline_object, "inline_object")
        elif TIME_RE.match(v):
            h, m, s, _, ms = TIME_RE.match(v).groups()
            time = datetime.time(int(h), int(m), int(s), int(ms) if ms else 0)
            return (time, "time")
        else:
            parsed_date = _loadDate(v)
            if parsed_date is not None:
                return (parsed_date, "date")
            if not strictlyValid:
                raise ValueError("Weirdness with leading zeroes or underscores in your number.")
            itype = "int"
            neg = False
            if v[0] == "-":
                neg = True
                v = v[1:]
            elif v[0] == "+":
                v = v[1:]
            v = v.replace("_", "")
            lowerv = v.lower()
            if "." in v or ("x" not in v and ("e" in v or "E" in v)):
                if "." in v and v.split(".", 1)[1] == "":
                    raise ValueError("This float is missing digits after the point")
                if v[0] not in "0123456789":
                    raise ValueError("This float doesn't have a leading digit")
                v = float(v)
                itype = "float"
            elif len(lowerv) == 3 and (lowerv == "inf" or lowerv == "nan"):
                v = float(v)
                itype = "float"
            if itype == "int":
                v = int(v, 0)
            if neg:
                return (0 - v, itype)
            return (v, itype)

    def boundedString(self, s):
        if len(s) == 0:
            return True
        if s[-1] != s[0]:
            return False
        i = -2
        backslash = False
        while len(s) + i > 0:
            if s[i] == "\\":
                backslash = not backslash
                i -= 1
            else:
                break
        return not backslash

    def _loadArrayIsStrArray(self, a):
        a = a[1:-1].strip()
        if a != "" and (a[0] == '"' or a[0] == "'"):
            return True
        return False

    def _splitArrayValues(self, arrayText):
        arrayText = arrayText[1:-1]
        elements = []
        startIndex = 0
        bracketDepth = 0
        braceDepth = 0
        quoteChar = ""
        tripleQuote = False
        backslash = False
        index = 0

        while index < len(arrayText):
            char = arrayText[index]
            if quoteChar:
                if quoteChar == '"' and char == "\\" and not backslash:
                    backslash = True
                    index += 1
                    continue
                if backslash:
                    backslash = False
                    index += 1
                    continue
                if tripleQuote:
                    if arrayText.startswith(quoteChar * 3, index):
                        quoteChar = ""
                        tripleQuote = False
                        index += 3
                        continue
                elif char == quoteChar:
                    quoteChar = ""
                    index += 1
                    continue
                index += 1
                continue

            if arrayText.startswith('"""', index):
                quoteChar = '"'
                tripleQuote = True
                index += 3
                continue
            if arrayText.startswith("'''", index):
                quoteChar = "'"
                tripleQuote = True
                index += 3
                continue
            if char == '"' or char == "'":
                quoteChar = char
                tripleQuote = False
                index += 1
                continue
            if char == "[":
                bracketDepth += 1
            elif char == "]":
                bracketDepth -= 1
            elif char == "{":
                braceDepth += 1
            elif char == "}":
                braceDepth -= 1
            elif char == "," and bracketDepth == 0 and braceDepth == 0:
                elements.append(arrayText[startIndex:index])
                startIndex = index + 1
            index += 1

        elements.append(arrayText[startIndex:])
        return elements

    def loadArray(self, a):
        retval = []
        a = a.strip()
        a = self._splitArrayValues(a)
        for i in range(len(a)):
            a[i] = a[i].strip()
            if a[i] != "":
                nval, ntype = self.loadValue(a[i])
                retval.append(nval)
        return retval


def _maskComments(text):
    charList = list(text)
    n = len(charList)
    scanIndex = 0
    openString = False
    openStrChar = ""
    multilineStr = False

    while scanIndex < n:
        char = charList[scanIndex]
        if char == "\r":
            charList[scanIndex] = " "
            scanIndex += 1
            continue

        if not openString:
            if text.startswith("/*", scanIndex):
                raise FinDecodeError("Block comments are not supported", text, scanIndex)
            if text.startswith("<!--", scanIndex):
                raise FinDecodeError("HTML comments are not supported", text, scanIndex)
            if char == "#" or (char == "/" and scanIndex + 1 < n and charList[scanIndex + 1] == "/"):
                while scanIndex < n and charList[scanIndex] != "\n":
                    charList[scanIndex] = " "
                    scanIndex += 1
                continue

        if char == "'" and openStrChar != '"':
            lookback = 1
            try:
                while charList[scanIndex - lookback] == "'":
                    lookback += 1
                    if lookback == 3:
                        break
            except IndexError:
                pass
            if lookback == 3:
                multilineStr = not multilineStr
                openString = multilineStr
            else:
                openString = not openString
            openStrChar = "'" if openString else ""
            scanIndex += 1
            continue

        if char == '"' and openStrChar != "'":
            oddbackslash = False
            lookback = 1
            tripquote = False
            try:
                while charList[scanIndex - lookback] == '"':
                    lookback += 1
                    if lookback == 3:
                        tripquote = True
                        break
                if lookback == 1 or (lookback == 3 and tripquote):
                    while charList[scanIndex - lookback] == "\\":
                        oddbackslash = not oddbackslash
                        lookback += 1
            except IndexError:
                pass
            if not oddbackslash:
                if tripquote:
                    multilineStr = not multilineStr
                    openString = multilineStr
                else:
                    openString = not openString
            openStrChar = '"' if openString else ""
            scanIndex += 1
            continue

        if openString and char == "\n" and not multilineStr:
            raise FinDecodeError("Unbalanced quotes", text, scanIndex)

        scanIndex += 1

    if openString:
        raise FinDecodeError("Unterminated string found. Reached end of file.", text, len(text))

    return charList


def _skipWhitespaceAndNewlines(charList, scanIndex):
    while scanIndex < len(charList) and charList[scanIndex] in " \t\r\n":
        scanIndex += 1
    return scanIndex


def _skipHorizontalWhitespace(charList, scanIndex):
    while scanIndex < len(charList) and charList[scanIndex] in " \t\r":
        scanIndex += 1
    return scanIndex


def _findStatementEnd(charList, startIndex, original):
    scanIndex = startIndex
    inString = False
    stringChar = ""
    inTriple = False
    tripleChar = ""
    arrayDepth = 0
    inlineTableDepth = 0

    while scanIndex < len(charList):
        char = charList[scanIndex]
        if inTriple:
            if (
                char == tripleChar
                and scanIndex + 2 < len(charList)
                and charList[scanIndex + 1] == tripleChar
                and charList[scanIndex + 2] == tripleChar
            ):
                backCount = 0
                backIndex = scanIndex - 1
                while backIndex >= startIndex and original[backIndex] == "\\":
                    backCount += 1
                    backIndex -= 1
                if backCount % 2 == 0:
                    inTriple = False
                    tripleChar = ""
                    scanIndex += 3
                    continue
            scanIndex += 1
            continue
        if inString:
            if char == "\\" and scanIndex + 1 < len(charList):
                scanIndex += 2
                continue
            if char == stringChar:
                inString = False
                stringChar = ""
            elif char == "\n":
                raise FinDecodeError("Unbalanced quotes", original, scanIndex)
            scanIndex += 1
            continue
        if char in ('"', "'"):
            if (
                scanIndex + 2 < len(charList)
                and charList[scanIndex + 1] == char
                and charList[scanIndex + 2] == char
            ):
                inTriple = True
                tripleChar = char
                scanIndex += 3
                continue
            inString = True
            stringChar = char
            scanIndex += 1
            continue
        if char == "[":
            arrayDepth += 1
            scanIndex += 1
            continue
        if char == "]":
            if arrayDepth > 0:
                arrayDepth -= 1
            scanIndex += 1
            continue
        if char == "{":
            inlineTableDepth += 1
            scanIndex += 1
            continue
        if char == "}":
            if inlineTableDepth > 0:
                inlineTableDepth -= 1
                scanIndex += 1
                continue
            return scanIndex
        if arrayDepth == 0 and inlineTableDepth == 0 and char in ("\n", ";"):
            return scanIndex
        scanIndex += 1

    if inTriple or inString:
        raise FinDecodeError("Unterminated string found. Reached end of file.", original, startIndex)
    return len(charList)


def _countTopLevelEquals(statementText):
    count = 0
    scanIndex = 0
    inString = False
    stringChar = ""
    inTriple = False
    tripleChar = ""
    arrayDepth = 0
    inlineTableDepth = 0

    while scanIndex < len(statementText):
        char = statementText[scanIndex]
        if inTriple:
            if (
                scanIndex + 2 < len(statementText)
                and statementText[scanIndex] == tripleChar
                and statementText[scanIndex + 1] == tripleChar
                and statementText[scanIndex + 2] == tripleChar
            ):
                inTriple = False
                tripleChar = ""
                scanIndex += 3
                continue
            scanIndex += 1
            continue
        if inString:
            if char == "\\" and scanIndex + 1 < len(statementText):
                scanIndex += 2
                continue
            if char == stringChar:
                inString = False
                stringChar = ""
            scanIndex += 1
            continue
        if char in ('"', "'"):
            if (
                scanIndex + 2 < len(statementText)
                and statementText[scanIndex + 1] == char
                and statementText[scanIndex + 2] == char
            ):
                inTriple = True
                tripleChar = char
                scanIndex += 3
                continue
            inString = True
            stringChar = char
            scanIndex += 1
            continue
        if char == "[":
            arrayDepth += 1
        elif char == "]":
            if arrayDepth > 0:
                arrayDepth -= 1
        elif char == "{":
            inlineTableDepth += 1
        elif char == "}":
            if inlineTableDepth > 0:
                inlineTableDepth -= 1
        elif char == "=" and arrayDepth == 0 and inlineTableDepth == 0:
            count += 1
        scanIndex += 1
    return count


def _parseBlockHeaderPath(headerText, original, pos):
    stripped = headerText.strip()
    if not stripped:
        raise FinDecodeError("Can't have a keygroup with an empty name", original, pos)

    isRelative = False
    if stripped.startswith(".."):
        raise FinDecodeError("Invalid group name '" + stripped + "'. Try quoting it.", original, pos)
    if stripped.startswith("."):
        isRelative = True
        stripped = stripped[1:].lstrip()
        if not stripped or stripped.startswith("."):
            raise FinDecodeError("Invalid group name '" + stripped + "'. Try quoting it.", original, pos)

    pathSegments = stripped.split(".")
    segmentIndex = 0
    while segmentIndex < len(pathSegments):
        pathSegments[segmentIndex] = pathSegments[segmentIndex].strip()
        if pathSegments[segmentIndex] and (
            pathSegments[segmentIndex][0] == '"' or pathSegments[segmentIndex][0] == "'"
        ):
            groupStr = pathSegments[segmentIndex]
            joinEnd = segmentIndex + 1
            while (not groupStr[0] == groupStr[-1]) or len(groupStr) == 1:
                joinEnd += 1
                if joinEnd > len(pathSegments) + 2:
                    raise FinDecodeError("Invalid group name '" + groupStr + "' Something went wrong.", original, pos)
                groupStr = ".".join(pathSegments[segmentIndex:joinEnd]).strip()
            try:
                pathSegments[segmentIndex] = _unescape(groupStr[1:-1])
            except ValueError as err:
                raise FinDecodeError("Invalid quoted segment: " + str(err), original, pos)
            pathSegments[segmentIndex + 1 : joinEnd] = []
        else:
            if not pathSegments[segmentIndex]:
                raise FinDecodeError("Can't have a keygroup with an empty name", original, pos)
            if not _groupNameRe.match(pathSegments[segmentIndex]):
                raise FinDecodeError(
                    "Invalid group name '" + pathSegments[segmentIndex] + "'. Try quoting it.",
                    original,
                    pos,
                )
        segmentIndex += 1

    return pathSegments, isRelative


def _openBlock(pathSegments, isRelative, isArrayOfTables, blockStack, rootTable, decoder, original, pos):
    if isRelative and isArrayOfTables:
        raise FinDecodeError("Relative array-of-tables are not supported", original, pos)
    if isRelative and not blockStack:
        raise FinDecodeError("[.name] at root is invalid", original, pos)
    if not isRelative:
        blockStack.clear()

    currentTable = blockStack[-1][1] if blockStack else rootTable
    for segmentIndex, segment in enumerate(pathSegments):
        isLast = segmentIndex == len(pathSegments) - 1
        if isArrayOfTables and isLast:
            existing = currentTable.get(segment)
            if existing is None:
                newElement = decoder.getEmptyTable()
                currentTable[segment] = [newElement]
            elif isinstance(existing, list):
                newElement = decoder.getEmptyTable()
                existing.append(newElement)
            else:
                raise FinDecodeError("Duplicate keys!", original, pos)
            blockStack.append((segment, currentTable[segment][-1]))
            currentTable = currentTable[segment][-1]
            continue

        existing = currentTable.get(segment)
        if existing is None:
            newTable = decoder.getEmptyTable()
            currentTable[segment] = newTable
            blockStack.append((segment, newTable))
            currentTable = newTable
        elif isinstance(existing, list):
            if not existing:
                existing.append(decoder.getEmptyTable())
            currentTable = existing[-1]
            blockStack.append((segment, currentTable))
        elif isinstance(existing, dict):
            currentTable = existing
            blockStack.append((segment, currentTable))
        else:
            raise FinDecodeError("Duplicate keys!", original, pos)


def _applyCloserSpec(spec, blockStack, original, pos):
    if spec == "":
        raise FinDecodeError("Invalid close directive.", original, pos)
    if spec == ".":
        if not blockStack:
            raise FinDecodeError("} [.] at root.", original, pos)
        blockStack.clear()
        return
    if spec == "..":
        if not blockStack:
            raise FinDecodeError("} at root.", original, pos)
        blockStack.pop()
        return
    if "." in spec:
        leftPart, _, rightPart = spec.partition(".")
        if rightPart == "":
            if not leftPart.isdigit():
                raise FinDecodeError("Invalid close directive.", original, pos)
            depth = int(leftPart)
            if depth <= 0:
                raise FinDecodeError("} [0.] invalid.", original, pos)
            if depth > len(blockStack):
                raise FinDecodeError("} [X.] where X exceeds stack depth.", original, pos)
            for _ in range(depth):
                blockStack.pop()
            return
        if leftPart == "":
            targetName = rightPart.strip()
            if not targetName:
                raise FinDecodeError("Invalid close directive.", original, pos)
            for frameIndex in range(len(blockStack) - 1, -1, -1):
                if blockStack[frameIndex][0] == targetName:
                    del blockStack[frameIndex:]
                    return
            raise FinDecodeError("} [.B] where no .B segment exists above.", original, pos)
        if not leftPart.isdigit() or int(leftPart) <= 0 or not rightPart.strip():
            raise FinDecodeError("Invalid close directive.", original, pos)
        extraLevels = int(leftPart)
        targetName = rightPart.strip()
        foundIndex = -1
        for frameIndex in range(len(blockStack) - 1, -1, -1):
            if blockStack[frameIndex][0] == targetName:
                foundIndex = frameIndex
                break
        if foundIndex == -1:
            raise FinDecodeError("} [X.B] target not found in current path.", original, pos)
        totalPop = (len(blockStack) - foundIndex) + extraLevels
        if totalPop > len(blockStack):
            raise FinDecodeError("Close directive exceeds stack depth.", original, pos)
        for _ in range(totalPop):
            blockStack.pop()
        return

    if not blockStack or blockStack[0][0] != spec:
        raise FinDecodeError("} [B] where B is not in current path at root.", original, pos)
    blockStack.clear()


def _parseBlockHeader(scanIndex, charList, original, blockStack, rootTable, decoder):
    startIndex = scanIndex
    isArrayOfTables = scanIndex + 1 < len(charList) and charList[scanIndex + 1] == "["
    scanIndex += 2 if isArrayOfTables else 1
    headerStart = scanIndex
    inString = False
    stringChar = ""

    while scanIndex < len(charList):
        char = charList[scanIndex]
        if inString:
            if char == "\\" and scanIndex + 1 < len(charList):
                scanIndex += 2
                continue
            if char == stringChar:
                inString = False
                stringChar = ""
            scanIndex += 1
            continue
        if char in ('"', "'"):
            inString = True
            stringChar = char
            scanIndex += 1
            continue
        if char == "\n":
            raise FinDecodeError("Opening key group bracket on line by itself.", original, startIndex)
        if isArrayOfTables:
            if char == "]" and scanIndex + 1 < len(charList) and charList[scanIndex + 1] == "]":
                break
        elif char == "]":
            break
        scanIndex += 1

    if scanIndex >= len(charList):
        raise FinDecodeError("Opening key group bracket on line by itself.", original, startIndex)

    headerText = "".join(charList[headerStart:scanIndex])
    scanIndex += 2 if isArrayOfTables else 1
    pathSegments, isRelative = _parseBlockHeaderPath(headerText, original, startIndex)
    _openBlock(pathSegments, isRelative, isArrayOfTables, blockStack, rootTable, decoder, original, startIndex)

    braceIndex = _skipWhitespaceAndNewlines(charList, scanIndex)
    if braceIndex >= len(charList) or charList[braceIndex] != "{":
        raise FinDecodeError("] without matching { on same opener.", original, braceIndex)
    return braceIndex + 1


def _parseCloser(scanIndex, charList, original, blockStack):
    startIndex = scanIndex
    if not blockStack:
        lookIndex = _skipHorizontalWhitespace(charList, scanIndex + 1)
        if lookIndex < len(charList) and charList[lookIndex] == "[":
            specEnd = lookIndex + 1
            while specEnd < len(charList) and charList[specEnd] != "]":
                if charList[specEnd] == "\n":
                    break
                specEnd += 1
            if specEnd < len(charList) and charList[specEnd] == "]":
                spec = "".join(charList[lookIndex + 1 : specEnd]).strip()
                if spec == ".":
                    raise FinDecodeError("} [.] at root.", original, startIndex)
        raise FinDecodeError("} at root.", original, startIndex)

    scanIndex += 1
    lookIndex = _skipHorizontalWhitespace(charList, scanIndex)
    if (
        lookIndex < len(charList)
        and charList[lookIndex] == "["
        and lookIndex + 1 < len(charList)
        and charList[lookIndex + 1] == "["
    ):
        blockStack.pop()
        return scanIndex
    if lookIndex < len(charList) and charList[lookIndex] == "[":
        specStart = lookIndex + 1
        specEnd = specStart
        while specEnd < len(charList) and charList[specEnd] != "]":
            if charList[specEnd] == "\n":
                raise FinDecodeError("Invalid close directive.", original, specStart)
            specEnd += 1
        if specEnd >= len(charList):
            raise FinDecodeError("Invalid close directive.", original, lookIndex)

        afterBracket = specEnd + 1
        peekIndex = _skipWhitespaceAndNewlines(charList, afterBracket)
        if peekIndex < len(charList) and charList[peekIndex] == "{":
            blockStack.pop()
            return scanIndex

        spec = "".join(charList[specStart:specEnd]).strip()
        _applyCloserSpec(spec, blockStack, original, startIndex)
        return specEnd + 1

    blockStack.pop()
    return scanIndex


def _parseAssignment(scanIndex, charList, original, blockStack, rootTable, decoder):
    startIndex = scanIndex
    endIndex = _findStatementEnd(charList, startIndex, original)
    statementText = "".join(charList[startIndex:endIndex]).strip()

    if not statementText:
        if endIndex < len(charList) and charList[endIndex] in ("\n", ";"):
            return endIndex + 1
        return endIndex

    equalsCount = _countTopLevelEquals(statementText)
    if equalsCount == 0:
        raise FinDecodeError("Key name found without value.", original, startIndex)
    if equalsCount > 1:
        raise FinDecodeError(
            "Key-value pair without \\n or ; terminator (and not immediately before }).",
            original,
            startIndex,
        )
    if endIndex < len(charList) and charList[endIndex] not in ("\n", ";", "}"):
        raise FinDecodeError(
            "Key-value pair without \\n or ; terminator (and not immediately before }).",
            original,
            endIndex,
        )

    keyText, _, valueText = statementText.partition("=")
    if not keyText.strip():
        raise FinDecodeError("Found empty keyname. ", original, startIndex)
    if not valueText.strip():
        raise FinDecodeError("Empty value is invalid", original, startIndex)

    currentTable = blockStack[-1][1] if blockStack else rootTable
    try:
        result = decoder.loadLine(statementText, currentTable, None, False)
    except (ValueError, IndexError, KeyError, TypeError) as err:
        raise FinDecodeError(str(err), original, startIndex)

    if result is not None:
        raise FinDecodeError("Unterminated string found. Reached end of file.", original, startIndex)

    if endIndex < len(charList) and charList[endIndex] in ("\n", ";"):
        return endIndex + 1
    return endIndex


def _parseFin(text, _dict=dict, decoder=None):
    if not isinstance(text, str):
        raise TypeError("Expecting something like a string")

    if decoder is None:
        decoder = FinDecoder(_dict)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    original = text
    rootTable = decoder.getEmptyTable()
    blockStack = []
    charList = _maskComments(text)
    scanIndex = 0

    while scanIndex < len(charList):
        scanIndex = _skipWhitespaceAndNewlines(charList, scanIndex)
        if scanIndex >= len(charList):
            break
        char = charList[scanIndex]
        if char == "[":
            scanIndex = _parseBlockHeader(scanIndex, charList, original, blockStack, rootTable, decoder)
        elif char == "}":
            scanIndex = _parseCloser(scanIndex, charList, original, blockStack)
        elif char == ";":
            scanIndex += 1
        elif char == "{":
            raise FinDecodeError("{ not following a [...] or =.", original, scanIndex)
        elif char == "]":
            raise FinDecodeError("Unexpected ']'", original, scanIndex)
        else:
            scanIndex = _parseAssignment(scanIndex, charList, original, blockStack, rootTable, decoder)

    blockStack.clear()
    return rootTable


def decode(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return _parseFin(fh.read())


decoded = decode(inputFile)
print(decoded)
