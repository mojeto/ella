from ella.newman.licenses.models import License
import logging

from django.conf import settings
from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.contrib.admin.options import InlineModelAdmin, IncorrectLookupParameters, FORMFIELD_FOR_DBFIELD_DEFAULTS
from django.forms.models import BaseInlineFormSet
from django import template
from django.http import HttpResponse, HttpResponseRedirect
from django.contrib.admin.views.main import ERROR_FLAG
from django.shortcuts import render_to_response
from django.db import transaction
from django.db.models import Q, ForeignKey, ManyToManyField, ImageField
from django.utils.functional import update_wrapper
from django.utils.translation import ugettext as _

from ella.core.cache.utils import get_cached_list
from ella.newman.changelist import NewmanChangeList, FilterChangeList
from ella.newman import models, fields, widgets, utils
from ella.newman.decorators import require_AJAX
from ella.newman.permission import is_category_model, model_category_fk, model_category_fk_value, applicable_categories
from ella.newman.permission import has_category_permission, get_permission, permission_filtered_model_qs, is_category_fk
from ella.newman.forms import DraftForm
from ella.newman.models import AdminHelpItem
from ella.newman.xoptions import XModelAdmin

DEFAULT_LIST_PER_PAGE = getattr(settings, 'NEWMAN_LIST_PER_PAGE', 25)

log = logging.getLogger('ella.newman')

def formfield_for_dbfield_factory(cls, db_field, **kwargs):
    formfield_overrides = dict(FORMFIELD_FOR_DBFIELD_DEFAULTS, **cls.formfield_overrides)
    custom_param_names = ('request', 'user', 'model', 'super_field', 'instance')
    custom_params = {}
    # move custom kwargs from kwargs to custom_params
    for key in kwargs:
        if key not in custom_param_names:
            continue
        custom_params[key] = kwargs[key]
        if key == 'request':
            custom_params['user'] = custom_params[key].user
    for key in custom_param_names:
        kwargs.pop(key, None)

    for css_class, rich_text_fields in getattr(cls, 'rich_text_fields', {}).iteritems():
        if db_field.name in rich_text_fields and custom_params['instance']:
            kwargs.update({
                'required': not db_field.blank,
                'label': db_field.verbose_name,
                'field_name': db_field.name,
                'instance': custom_params['instance'],
            })
            print db_field, db_field.name , custom_params['instance'] , custom_params['model']
            rich_text_field = fields.RichTextField(**kwargs)
            print '---'
            if css_class:
                rich_text_field.widget.attrs['class'] += ' %s' % css_class
            return rich_text_field

    if isinstance(db_field, ImageField):
        # we accept only (JPEG) images with RGB color profile.
        return fields.RGBImageField(db_field, **kwargs)

    if db_field.name in cls.raw_id_fields and isinstance(db_field, ForeignKey):
        kwargs['widget'] = widgets.ForeignKeyRawIdWidget(db_field.rel)
        return db_field.formfield(**kwargs)

    if db_field.name in getattr(cls, 'suggest_fields', {}).keys() \
                        and isinstance(db_field, (ForeignKey, ManyToManyField)):
        kwargs.update({
            'required': not db_field.blank,
            'label': db_field.verbose_name,
            'model': cls.model,
            'lookup': cls.suggest_fields[db_field.name]
        })
        return fields.AdminSuggestField(db_field, **kwargs)
    # magic around restricting category choices in all ForeignKey (related to Category) fields
    if is_category_fk(db_field) and 'model' in custom_params:
        kwargs.update({
            'model': custom_params['model'],
            'user': custom_params['user']
        })
        super_qs = custom_params['super_field'].queryset
        return fields.CategoryChoiceField(super_qs, **kwargs)

    if db_field.__class__ in formfield_overrides:
        kwargs = dict(formfield_overrides[db_field.__class__], **kwargs)
        return db_field.formfield(**kwargs)

    return db_field.formfield(**kwargs)


#class NewmanModelAdmin(admin.ModelAdmin):
class NewmanModelAdmin(XModelAdmin):
    changelist_view_cl = NewmanChangeList

    def __init__(self, *args, **kwargs):
        super(NewmanModelAdmin, self).__init__(*args, **kwargs)
        self.list_per_page = DEFAULT_LIST_PER_PAGE
        self.model_content_type = ContentType.objects.get_for_model(self.model)

    def get_form(self, request, obj=None, **kwargs):
        self._magic_instance = obj # adding edited object to ModelAdmin instance.
        return super(NewmanModelAdmin, self).get_form(request, obj, **kwargs)

    def get_urls(self):
        from django.conf.urls.defaults import patterns, url

        def wrap(view):
            def wrapper(*args, **kwargs):
                return self.admin_site.admin_view(view)(*args, **kwargs)
            return update_wrapper(wrapper, view)

        info = self.admin_site.name, self.model._meta.app_label, self.model._meta.module_name

        urlpatterns = patterns('',
            url(r'^suggest/$',
                wrap(self.suggest_view),
                name='%sadmin_%s_%s_suggest' % info),
            url(r'^filters/$',
                wrap(self.filters_view),
                name='%sadmin_%s_%s_filters' % info),
            url(r'^(.+)/draft/save/$',
                wrap(self.save_draft_view),
                name='%sadmin_%s_%s_save_draft' % info),
            url(r'^(.+)/draft/load/$',
                wrap(self.load_draft_view),
                name='%sadmin_%s_%s_load_draft' % info),
        )
        urlpatterns += super(NewmanModelAdmin, self).get_urls()
        return urlpatterns

    @require_AJAX
    def save_draft_view(self, request, extra_context=None):
        """ Autosave data (dataloss-prevention) or save as (named) template """
        # TODO: clean too old autosaved... (keep last 3-5 autosaves)
        # jQuery.post( 'http://localhost:8000/articles/article/1503079/draft/save/', {'data': '{"title": "Jarni style", "slug": "jarni-style" }'} )
        self.register_newman_variables(request)
        data = request.POST.get('data', None)
        if not data:
            return HttpResponse(content=_('No data passed in POST variable "data".'), mimetype='text/plain', status=405)
        title = request.POST.get('title', '')
        id = request.POST.get('id', None)

        if id:
            try:
                obj = models.AdminUserDraft.objects.get(pk=id)
                obj.data = data
                obj.title = title
                obj.save()
            except:
                obj = models.AdminUserDraft.objects.create(
                    ct=self.model_content_type,
                    user=request.user,
                    data=data,
                    title=title
                )
        else:
            obj = models.AdminUserDraft.objects.create(
                ct=self.model_content_type,
                user=request.user,
                data=data,
                title=title
            )
        data = {'id': obj.pk, 'title': obj.__unicode__()}
        return utils.JsonResponse(_('Preset %s was saved.' % obj.__unicode__()), data)

    @require_AJAX
    def load_draft_view(self, request, extra_context=None):
        """ Returns draft identified by request.GET['id'] variable. """
        self.register_newman_variables(request)
        id = request.GET.get('id', None)
        if not id:
            return utils.JsonResponse(_('No id found in GET variable "id".'), status=405)
        drafts = models.AdminUserDraft.objects.filter(
            ct=self.model_content_type,
            user=request.user,
            pk=id
        )
        if not drafts:
            return utils.JsonResponse(_('No matching draft found.'), status=404)
        draft = drafts[0]
        return utils.JsonResponse(_('Loading draft "%s"...' % draft.__unicode__()), draft.data)

    @require_AJAX
    def filters_view(self, request, extra_context=None):
        "stolen from: The 'change list' admin view for this model."
        self.register_newman_variables(request)
        opts = self.model._meta
        app_label = opts.app_label
        try:
            cl = FilterChangeList(request, self.model, self.list_display, self.list_display_links, self.list_filter,
                self.date_hierarchy, self.search_fields, self.list_select_related, self.list_per_page, self.list_editable, self)
        except IncorrectLookupParameters:
            # Wacky lookup parameters were given, so redirect to the main
            # changelist page, without parameters, and pass an 'invalid=1'
            # parameter via the query string. If wacky parameters were given and
            # the 'invalid=1' parameter was already in the query string, something
            # is screwed up with the database, so display an error page.
            if ERROR_FLAG in request.GET.keys():
                return render_to_response('admin/invalid_setup.html', {'title': _('Database error')})
            return HttpResponseRedirect(request.path + '?' + ERROR_FLAG + '=1')
        cl.formset = None

        context = {
            'title': cl.title,
            'is_popup': cl.is_popup,
            'cl': cl,
            'has_add_permission': self.has_add_permission(request),
            'root_path': self.admin_site.root_path,
            'app_label': app_label,
        }
        context.update(extra_context or {})
        out= render_to_response(
            'admin/filters.html',
            context,
            context_instance=template.RequestContext(request)
        )
        return HttpResponse(out, mimetype='text/plain;charset=utf-8')

    @require_AJAX
    def changelist_view(self, request, extra_context=None):
        self.register_newman_variables(request)
        opts = self.model._meta
        app_label = opts.app_label
        context = super(NewmanModelAdmin, self).get_changelist_context(request)
        if type(context) != dict:
            return context

        if context['media']:
            raw_media = self.prepare_media(context['media'])
            context['media'] = raw_media

        context.update(extra_context or {})
        return render_to_response(self.change_list_template or [
            'admin/%s/%s/change_list.html' % (app_label, opts.object_name.lower()),
            'admin/%s/change_list.html' % app_label,
            'admin/change_list.html'
        ], context, context_instance=template.RequestContext(request))

    @require_AJAX
    def suggest_view(self, request, extra_context=None):
        self.register_newman_variables(request)
        SUGGEST_VIEW_LIMIT = getattr(settings, 'SUGGEST_VIEW_LIMIT', 20)
        SUGGEST_VIEW_MIN_LENGTH = getattr(settings, 'SUGGEST_VIEW_MIN_LENGTH', 2)
        SUGGEST_RETURN_ALL_FIELD = getattr(settings, 'SUGGEST_RETURN_ALL_FIELD', True)
        if not ('f' in request.GET.keys() and 'q' in request.GET.keys()):
            raise AttributeError, 'Invalid query attributes. Example: ".../?f=field_a&f=field_b&q=search_term&o=offset"'
        elif len(request.GET.get('q')) < SUGGEST_VIEW_MIN_LENGTH:
            return HttpResponse( '', mimetype='text/plain;charset=utf-8' )

        offset = 0
        if 'o' in request.GET.keys() and request.GET.get('o'):
            offset = int(request.GET.get('o'))
        limit = offset + SUGGEST_VIEW_LIMIT
        lookup_fields = [u'id'] + request.GET.getlist('f')
        lookup_value = request.GET.get('q')
        lookup = None

        model_fields = [f.name for f in self.model._meta.fields]

        for f in lookup_fields:
            if not (f in model_fields or f.split('__')[0] in model_fields):
                raise AttributeError, 'Model "%s" has not field "%s". Possible fields are "%s".' \
                                    % (self.model._meta.object_name, f, ', '.join(model_fields))
            lookup_key = str('%s__icontains' % f)
            if not lookup:
                lookup = Q(**{lookup_key: lookup_value})
            else:
                lookup = lookup | Q(**{lookup_key: lookup_value})
        # user role based category filtering
        if not is_category_model(self.model):
            category_field = model_category_fk(self.model)
            if category_field and request.user:
                applicable = applicable_categories(request.user)
                args_lookup = { '%s__in' % category_field.name: applicable}
                lookup = lookup & Q(**args_lookup)
        else:
            applicable = applicable_categories(request.user)
            lookup = lookup & Q(pk__in=applicable)
        # user category filter
        qs = utils.user_category_filter(self.model.objects.filter(lookup), request.user)

        if SUGGEST_RETURN_ALL_FIELD:
            data = qs.values(*lookup_fields)
        else:
            data = qs.filter(lookup).values(*lookup_fields[:2])

        # sort the suggested items so that those starting with the sought term come first
        def compare(a,b):
            def _cmp(a,b,sought):
                a_starts = unicode(a).lower().startswith(sought)
                b_starts = unicode(b).lower().startswith(sought)
                # if exactly one of (a,b) starts with sought, the one starting with it comes first
                if a_starts ^ b_starts:
                    if a_starts: return -1
                    if b_starts: return +1
                # else compare lexicographically
                return cmp(a,b)
            return _cmp(a,b,unicode(lookup_value).lower())
        cnt = len(data)
        data = list(data)
        if offset >= len(data): return HttpResponse('SPECIAL: OFFSET OUT OF RANGE', mimetype='text/plain')
        data.sort(cmp=compare, key=lambda x: x[lookup_fields[1]])
        data = data[offset:limit]

        ft = []
        ft.append('{cnt:%d}' % cnt)
        for item in data:
            if SUGGEST_RETURN_ALL_FIELD:
                ft.append( "%s".encode('utf-8') % '|'.join("%s" % item[f] for f in lookup_fields) )
            else:
                ft.append( "%s".encode('utf-8') % '|'.join("%s" % item[f] for f in lookup_fields[:2]) )

        return HttpResponse( '\n'.join(ft), mimetype='text/plain;charset=utf-8' )

    def register_newman_variables(self, request):
        self.user = request.user

    def has_view_permission(self, request, obj):
        opts = self.opts
        view_perm = '%s.view_%s' % ( opts.app_label, opts.object_name.lower() )
        return request.user.has_perm(view_perm)

    def has_model_view_permission(self, request, obj=None):
        """ returns True if user has permission to view this model, otherwise False. """
        # try to find view or change perm. for given user in his permissions or groups permissions
        can_change = super(NewmanModelAdmin, self).has_change_permission(request, obj)
        can_view = self.has_view_permission(request, obj)
        if can_view or can_change:
            return True

        # find permission to view or change in CategoryUserRoles for given user
        user = request.user
        opts = self.opts
        change_perm = '%s.%s' % ( opts.app_label, opts.get_change_permission() )
        view_perm = '%s.view_%s' % ( opts.app_label, opts.object_name.lower() )
        perms = [change_perm, view_perm]
        for role in user.categoryuserrole_set.all():
            for perm in role.group.permissions.all():
                p = '%s.%s' % ( opts.app_label, perm.codename )
                if p in perms:
                    return True
        # no permission found
        return False

    def has_change_permission(self, request, obj=None):
        """
        Returns True if the given request has permission to change the given
        Django model instance.

        If `obj` is None, this should return True if the given request has
        permission to change *any* object of the given type.

        If request is GET type, at least view_permission is needed. In case
        of POST request change permission is needed.
        """
        cfield = model_category_fk_value(obj)
        if obj is None or not cfield:
            if request.method == 'POST':
                return super(NewmanModelAdmin, self).has_change_permission(request, obj)
            else:
                return self.has_model_view_permission(request, obj)

        opts = self.opts
        change_perm = '%s.%s' % ( opts.app_label, opts.get_change_permission() )
        view_perm = '%s.view_%s' % ( opts.app_label, opts.object_name.lower() )
        can_view = has_category_permission( request.user, cfield, view_perm )
        can_change = has_category_permission( request.user, cfield, change_perm )

        if request.method == 'POST' and can_change:
            return True
        elif request.method == 'GET' and (can_view or can_change):
            return True
        return False

    def has_delete_permission(self, request, obj=None):
        """
        Returns True if the given request has permission to change the given
        Django model instance.

        If `obj` is None, this should return True if the given request has
        permission to delete *any* object of the given type.
        """
        opts = self.opts
        del_perm = opts.app_label + '.' + opts.get_delete_permission()
        if request.user.has_perm(del_perm):
            return True
        user = request.user
        for role in user.categoryuserrole_set.all():
            if del_perm in role.group.permissions.all():
                return True
        # no permission found
        return False

    def get_change_view_inline_formsets(self, request, obj, formsets, media):
        inline_admin_formsets = []
        self._raw_inlines = {}
        for inline, formset in zip(self.inline_instances, formsets):
            self._raw_inlines[str(inline.model._meta).replace('.', '__')] = formset
            fieldsets = list(inline.get_fieldsets(request, obj))
            inline_admin_formset = admin.helpers.InlineAdminFormSet(inline, formset, fieldsets)
            inline_admin_formsets.append(inline_admin_formset)
            media = media + inline_admin_formset.media
        return inline_admin_formsets, media

    @require_AJAX
    @transaction.commit_on_success
    def change_view(self, request, object_id, extra_context=None):
        "The 'change' admin view for this model."
        self.register_newman_variables(request)
        out = self.get_change_view_context(request, object_id)
        if type(out) != dict:
            # context is not a dict probably HttpReponseRedirect, or Http404 etc.
            return out
        context = out

        # === newman specific

        # dynamic heelp messages
        help_qs = get_cached_list(AdminHelpItem, ct=self.model_content_type, lang=settings.LANGUAGE_CODE)
        form  = context['raw_form']
        for msg in help_qs:
            try:
                form.fields[msg.field].hint_text = msg.short
                form.fields[msg.field].help_text = msg.long
            except KeyError:
                pass

        # raw forms for JS manipulations
        raw_frm_all = {
            'form': form,
            'inlines': self._raw_inlines
        }

        # form for autosaved and draft objects
        draft_form = DraftForm(user=request.user, content_type=self.model_content_type)

        context.update({
            'media': self.prepare_media(context['media']),
            'raw_form': raw_frm_all,
            'draft_form': draft_form,
        })

        # === end of newman specific
        context.update(extra_context or {})
        obj = self.get_change_view_object(object_id)
        return self.render_change_form(request, context, change=True, obj=obj)

    @transaction.commit_on_success
    def add_view(self, request, form_url='', extra_context=None):
        "The 'add' admin view for this model."
        self.register_newman_variables(request)
        context = self.get_add_view_context(request, form_url)

        # form for autosaved and draft objects
        draft_form = DraftForm(user=request.user, content_type=self.model_content_type)

        context.update({
            'media': self.prepare_media(context['media']),
            'draft_form': draft_form,
        })
        context.update(extra_context or {})
        if 'object_added' in context:
            msg = request.user.message_set.all()[0].message
            return utils.JsonResponse(msg, {'id': context['object'].pk})
        elif 'error_dict' in context:
            msg = _('Please correct errors in form')
            return utils.JsonResponse(msg, {}, context['error_dict'], 406)
        else:
            return self.render_change_form(request, context, add=True)

    def formfield_for_dbfield(self, db_field, **kwargs):
        if is_category_fk(db_field):
            kwargs['super_field'] = super(NewmanModelAdmin, self).formfield_for_dbfield(db_field, **kwargs)
        kwargs.update({
            'model': self.model,
            'user': self.user,
            'instance': self._magic_instance,
        })
        return formfield_for_dbfield_factory(self, db_field, **kwargs)

    def queryset(self, request):
        """
        First semi-working draft of category-based permissions. It will allow permissions to be set per category
        effectively hiding the content the user has no permission to see/change.
        """
        q = super(NewmanModelAdmin, self).queryset(request)
        # user category filter
        qs = utils.user_category_filter(q, request.user)

        # if self.model is licensed filter queryset
        if 'ella.newman.licenses' in settings.INSTALLED_APPS:
            exclude_pks = License.objects.unapplicable_for_model(self.model)
            qs = qs.exclude(id__in=exclude_pks)

        if request.user.is_superuser:
            return qs
        view_perm = self.opts.app_label + '.' + 'view_' + self.model._meta.module_name.lower()
        change_perm = self.opts.app_label + '.' + 'change_' + self.model._meta.module_name.lower()
        perms = (view_perm, change_perm,)
#        return permission_filtered_model_qs(qs, request.user, perms)
        return permission_filtered_model_qs(qs, request.user, perms)


    def prepare_media(self, standard_media):
        """ Returns raw media paths for ajax loading """

        raw_media = []
        if standard_media._css.has_key('screen'):
            raw_media.extend(standard_media._css['screen'])
        raw_media.extend(standard_media._js)
        return raw_media

class NewmanInlineFormSet(BaseInlineFormSet):
    def get_queryset(self):
        user = self.form._magic_user
        if not hasattr(self, '_queryset'):
            if self.queryset is not None:
                qs = self.queryset
            else:
                qs = self.model._default_manager.get_query_set()
            # category based permissions
            if not user.is_superuser:
                category_fk = model_category_fk(self.model)
                if category_fk:
                    # in ListingInlineOptions: self.instance .. Placement instance, self.model .. Listing
                    view_perm = get_permission('view', self.model)
                    change_perm = get_permission('change', self.model)
                    perms = (view_perm, change_perm,)
                    qs = permission_filtered_model_qs(qs, user, perms)
            # user filtered categories
            qs = utils.user_category_filter(qs, user)

            if self.max_num > 0:
                self._queryset = qs[:self.max_num]
            else:
                self._queryset = qs
        return self._queryset

class NewmanInlineModelAdmin(InlineModelAdmin):
    formset = NewmanInlineFormSet

    def get_formset(self, request, obj=None):
        setattr(self.form, '_magic_user', request.user) # magic variable assigned to form
        setattr(self, '_magic_user', request.user) # magic variable
        from django.forms.models import _get_foreign_key
        setattr(self, '_magic_instance', obj)
        setattr(self, '_magic_fk', _get_foreign_key(self.parent_model, self.model, fk_name=self.fk_name))
        self.user = request.user
        return super(NewmanInlineModelAdmin, self).get_formset(request, obj)

    def formfield_for_dbfield(self, db_field, **kwargs):
        if is_category_fk(db_field):
            kwargs['super_field'] = super(NewmanInlineModelAdmin, self).formfield_for_dbfield(db_field, **kwargs)
        inst = None
        # Inlined object is requested by RichTextField (the field needs to lookup SrcText)
        if hasattr(self, '_magic_instance') and self._magic_instance:
            inst = self.model.objects.get(**{self._magic_fk.name: self._magic_instance.pk})
        kwargs.update({
            'model': self.model,
            'user': self._magic_user,
            'instance': inst,
        })
        return formfield_for_dbfield_factory(self, db_field, **kwargs)

class NewmanStackedInline(NewmanInlineModelAdmin):
    template = 'admin/edit_inline/stacked.html'

class NewmanTabularInline(NewmanInlineModelAdmin):
    template = 'admin/edit_inline/tabular.html'
