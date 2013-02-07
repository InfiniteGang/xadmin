# coding=utf-8
"""
数据过滤器
==========

功能
----

在数据列表页面提供数据过滤功能, 包括: 模糊搜索, 数字范围搜索, 日期搜索等等

截图
----

.. image:: /images/plugins/filter.png

使用
----

在 Model OptionClass 中设置以下属性:

    * ``list_filter`` 属性:

        该属性指定可以过滤的列的名字, 系统会自动生成搜索器

    * ``search_fields`` 属性:

        属性指定可以通过搜索框搜索的数据列的名字, 搜索框搜索使用的是模糊查找的方式, 一般用来搜素名字等字符串字段

    * ``free_query_filter`` 属性:

        默认为 ``True`` , 指定是否可以自由搜索. 如果开启自有搜索, 用户可以通过 url 参数来进行特定的搜索, 例如::

            http://xxx.com/xadmin/auth/user/?name__contains=tony

使用过滤器的例子::

    class UserAdmin(object):
        list_filter = ('is_staff', 'is_superuser', 'is_active')
        search_fields = ('username', 'first_name', 'last_name', 'email')

版本
----

暂无

制作过滤器
-----------

您也可以制作自己的过滤器, 用来进行一些特定的过滤. 过滤器需要继承 :class:`exadmin.filters.BaseFilter` 类, 
并使用 :attr:`exadmin.filters.manager` 注册过滤器.



"""
import operator
from exadmin import widgets

from exadmin.util import get_fields_from_path, lookup_needs_distinct
from django.core.exceptions import SuspiciousOperation, ImproperlyConfigured
from django.db import models
from django.db.models.fields import FieldDoesNotExist
from django.db.models.related import RelatedObject
from django.db.models.sql.constants import LOOKUP_SEP, QUERY_TERMS
from django.template import loader
from django.utils.encoding import smart_str

from exadmin.filters import manager as filter_manager, FILTER_PREFIX, SEARCH_VAR, DateFieldListFilter, RelatedFieldSearchFilter
from exadmin.sites import site
from exadmin.views import BaseAdminPlugin, ListAdminView

class IncorrectLookupParameters(Exception):
    pass

class FilterPlugin(BaseAdminPlugin):
    list_filter = ()
    search_fields = ()
    free_query_filter = True

    def lookup_allowed(self, lookup, value):
        model = self.model
        # Check FKey lookups that are allowed, so that popups produced by
        # ForeignKeyRawIdWidget, on the basis of ForeignKey.limit_choices_to,
        # are allowed to work.
        for l in model._meta.related_fkey_lookups:
            for k, v in widgets.url_params_from_lookup_dict(l).items():
                if k == lookup and v == value:
                    return True

        parts = lookup.split(LOOKUP_SEP)

        # Last term in lookup is a query term (__exact, __startswith etc)
        # This term can be ignored.
        if len(parts) > 1 and parts[-1] in QUERY_TERMS:
            parts.pop()

        # Special case -- foo__id__exact and foo__id queries are implied
        # if foo has been specificially included in the lookup list; so
        # drop __id if it is the last part. However, first we need to find
        # the pk attribute name.
        rel_name = None
        for part in parts[:-1]:
            try:
                field, _, _, _ = model._meta.get_field_by_name(part)
            except FieldDoesNotExist:
                # Lookups on non-existants fields are ok, since they're ignored
                # later.
                return True
            if hasattr(field, 'rel'):
                model = field.rel.to
                rel_name = field.rel.get_related_field().name
            elif isinstance(field, RelatedObject):
                model = field.model
                rel_name = model._meta.pk.name
            else:
                rel_name = None
        if rel_name and len(parts) > 1 and parts[-1] == rel_name:
            parts.pop()

        if len(parts) == 1:
            return True
        clean_lookup = LOOKUP_SEP.join(parts)
        return clean_lookup in self.list_filter

    def get_list_queryset(self, queryset):
        lookup_params = dict([(smart_str(k)[len(FILTER_PREFIX):],v) for k,v in self.admin_view.params.items() \
            if smart_str(k).startswith(FILTER_PREFIX) and v != ''])
        use_distinct = False

        # for clean filters
        self.admin_view.has_query_param = bool(lookup_params)
        self.admin_view.clean_query_url = self.admin_view.get_query_string(remove=\
                [k for k in self.request.GET.keys() if k.startswith(FILTER_PREFIX)])

        # Normalize the types of keys
        if not self.free_query_filter:
            for key, value in lookup_params.items():
                if not self.lookup_allowed(key, value):
                    raise SuspiciousOperation("Filtering by %s not allowed" % key)

        self.filter_specs = []
        if self.list_filter:
            for list_filter in self.list_filter:
                if callable(list_filter):
                    # This is simply a custom list filter class.
                    spec = list_filter(self.request, lookup_params,
                        self.model, self)
                else:
                    field_path = None
                    if isinstance(list_filter, (tuple, list)):
                        # This is a custom FieldListFilter class for a given field.
                        field, field_list_filter_class = list_filter
                    else:
                        # This is simply a field name, so use the default
                        # FieldListFilter class that has been registered for
                        # the type of the given field.
                        field, field_list_filter_class = list_filter, filter_manager.create
                    if not isinstance(field, models.Field):
                        field_path = field
                        field = get_fields_from_path(self.model, field_path)[-1]
                    spec = field_list_filter_class(field, self.request, lookup_params,
                        self.model, self.admin_view, field_path=field_path)
                    # Check if we need to use distinct()
                    use_distinct = (use_distinct or
                                    lookup_needs_distinct(self.opts, field_path))
                if spec and spec.has_output():
                    new_qs = spec.do_filte(queryset)
                    if new_qs is not None:
                        queryset = new_qs
                    self.filter_specs.append(spec)

        self.has_filters = bool(self.filter_specs)
        self.admin_view.filter_specs = self.filter_specs
        self.admin_view.used_filter_num = len(filter(lambda f: f.is_used, self.filter_specs))

        try:
            for key, value in lookup_params.items():
                use_distinct = (use_distinct or lookup_needs_distinct(self.opts, key))
        except FieldDoesNotExist, e:
            raise IncorrectLookupParameters(e)

        try:
            queryset = queryset.filter(**lookup_params)
        except (SuspiciousOperation, ImproperlyConfigured):
            raise
        except Exception, e:
            raise IncorrectLookupParameters(e)

        query = self.request.GET.get(SEARCH_VAR, '')

        # Apply keyword searches.
        def construct_search(field_name):
            if field_name.startswith('^'):
                return "%s__istartswith" % field_name[1:]
            elif field_name.startswith('='):
                return "%s__iexact" % field_name[1:]
            elif field_name.startswith('@'):
                return "%s__search" % field_name[1:]
            else:
                return "%s__icontains" % field_name

        if self.search_fields and query:
            orm_lookups = [construct_search(str(search_field))
                           for search_field in self.search_fields]
            for bit in query.split():
                or_queries = [models.Q(**{orm_lookup: bit})
                              for orm_lookup in orm_lookups]
                queryset = queryset.filter(reduce(operator.or_, or_queries))
            if not use_distinct:
                for search_spec in orm_lookups:
                    if lookup_needs_distinct(self.opts, search_spec):
                        use_distinct = True
                        break
            self.admin_view.search_query = query

        if use_distinct:
            return queryset.distinct()
        else:
            return queryset

    # Media
    def get_media(self, media):
        if bool(filter(lambda s: isinstance(s, DateFieldListFilter), self.filter_specs)):
            media.add_js([self.static('exadmin/js/date.js')])
            media.add_js([self.static('exadmin/js/daterangepicker.js')])
            media.add_js([self.static('exadmin/js/bootstrap-datepicker.js')])
            media.add_css({'screen': [self.static('exadmin/css/daterangepicker.css')]})
        if bool(filter(lambda s: isinstance(s, RelatedFieldSearchFilter), self.filter_specs)):
            media.add_js([self.static('exadmin/js/select2.js')])
            media.add_js([self.static('exadmin/js/form.js')])
            media.add_css({'screen': [self.static('exadmin/css/select2.css')]})
        media.add_js([self.static('exadmin/js/filters.js')])
        return media

    # Block Views
    def block_nav_menu(self, context, nodes):
        if self.has_filters:
            nodes.append(loader.render_to_string('admin/filters.html', context_instance=context))

    def block_nav_form(self, context, nodes):
        if self.search_fields:
            nodes.append(loader.render_to_string('admin/blocks/search_form.html', \
                {'search_var': SEARCH_VAR, 
                'remove_search_url': self.admin_view.get_query_string(remove=[SEARCH_VAR]),
                'search_form_params': self.admin_view.get_form_params(remove=[SEARCH_VAR])}, \
                context_instance=context))

site.register_plugin(FilterPlugin, ListAdminView)


