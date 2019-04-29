#
#  Copyright (C) 2019 Bloomberg Finance LP
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library. If not, see <http://www.gnu.org/licenses/>.
#
#  Authors:
#        Jonathan Maw <jonathan.maw@codethink.co.uk>


from ..types import Scope
from .cachekeycontroller import CacheKeyController


# StrictCacheKeyController()
#
# The StrictCacheKeyController is an object that handles the calculation
# of strict cache keys.
class StrictCacheKeyController(CacheKeyController):
    # calculate_strong_key()
    #
    # Calculates the strong cache key
    #
    # args:
    #    element (Element): the element object
    #
    # returns:
    #    (str): The key generated, or None
    #
    def calculate_strong_key(self, element):
        if self._strong_key is None:
            dependencies = [
                e._get_strict_cache_key() for e in element.dependencies(Scope.BUILD)
            ]
            self._strong_key = self._calculate_cache_key(element, dependencies)

        return self._strong_key
