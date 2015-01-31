#coding: utf-8
from __future__ import unicode_literals
import datetime

from django.db import models
from django.utils import six
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from django.db.models import Sum
from django.utils.timezone import now
from dateutil import parser as dateparser
from django_extensions.db.fields import ShortUUIDField

from users.models import Player
from .aggregates import SumWithDefault

from django.conf import settings

import logging


logger = logging.getLogger('geekcard')

# from django_select2.fields import ModelSelect2Field
try:
    from lxml import etree
except ImportError:
    try:
        import xml.etree.cElementTree as etree
    except ImportError:
        import xml.etree.ElementTree as etree

PARSER_CHOICES = (
    (1, _("Wizard's Event Reporter 4.x")),
    (2, _('Pokemon Reporter (NOT IMPLEMENTED!)')),
)


@python_2_unicode_compatible
class Game(models.Model):
    """
    Gra jest czymś innym niż liga do danej gry.
    Przykładowo w ramach jednej gry może być zorganizowanych kilka lig.
    """
    uuid = ShortUUIDField()
    name = models.CharField(_('name'), max_length=64)
    slug = models.SlugField(_('slug'))
    id_name = models.CharField(_('ID Name'), max_length=64, blank=True, help_text=_('Eg. "DCI Number"'))
    points_for_winning = models.PositiveSmallIntegerField(_('points for winning'), default=3)
    points_for_losing = models.PositiveSmallIntegerField(_('points for losing'), default=0)
    points_for_draw = models.PositiveSmallIntegerField(_('points for draw'), default=1)
    rewards = models.ManyToManyField('RewardCategory', verbose_name=_('rewards'))
    reporter_tool = models.PositiveSmallIntegerField(default=1, choices=PARSER_CHOICES)

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class GameID(models.Model):
    """
    Numer identyfikacyjny w ramach jakiejś gry (np. numer DCI)
    """
    uuid = ShortUUIDField()
    game = models.ForeignKey(Game, verbose_name=_('game'))  # numer identyfikacyjny jest jeden dla danej gry
    player = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_('player'))
    number = models.CharField(_('number'), max_length=64, db_index=True)
    # czy zrobić unikalność pary gracz-gra? - wymusza to jeden numer ID na daną grę

    class Meta:
        # unikalność identyfikatora w ramach danej gry. uniemożliwia kilkukrotne wprowadzanie tego samego numeru
        unique_together = ('game', 'number',)

    def __str__(self):
        return "%s %s: %s" % (self.player, self.game.id_name, self.number)


@python_2_unicode_compatible
class RewardCategory(models.Model):
    """
    Zakup boostera, udział w turnieju, wygrany mecz, przegrany mecz.
    """
    uuid = ShortUUIDField()
    name = models.CharField(_('name'), max_length=64, db_index=True)
    value = models.SmallIntegerField(_('value'))  # negatyw za odbiór promki
    eternal = models.BooleanField(_('eternal'), default=False,
                                  help_text=_('Eternal rewards do not disappear over time.'))

    def __str__(self):
        return "%s (%s)" % (self.name, self.value)


@python_2_unicode_compatible
class LeagueSeason(models.Model):
    """
    Co sezon liga niejako startuje na nowo.
    """
    uuid = ShortUUIDField()
    name = models.CharField(_('name'), max_length=64)
    slug = models.SlugField(_('slug'), unique=True, db_index=True)
    game = models.ForeignKey(Game, verbose_name=_('game'))
    start_date = models.DateField(_('start date'))
    end_date = models.DateField(_('end date'))
    max_matches = models.PositiveSmallIntegerField(_('max matches per pair'), default=2,
                                                   help_text=_('Players may play max this number of matches per season.'))
    players = models.ManyToManyField(settings.AUTH_USER_MODEL, verbose_name=_('player'), through='LeagueEnroll')
    win_reward = models.ForeignKey(RewardCategory, related_name='conf_as_win', verbose_name=_('win reward'), blank=True,
                                   null=True)
    lose_reward = models.ForeignKey(RewardCategory, related_name='conf_as_lose', verbose_name=_('lose reward'),
                                    blank=True, null=True)
    draw_reward = models.ForeignKey(RewardCategory, related_name='conf_as_draw', verbose_name=_('draw reward'),
                                    blank=True, null=True)
    default_match_category = models.ForeignKey('EventCategory', verbose_name=_('default event category'),
                                               null=True, blank=True, help_text=_('Default category for off-tournament matches.'))
    # extra_rewards = models.ManyToManyField(RewardCategory, verbose_name=_('extra rewards'),
    #                                        help_text=_('additional rewards available in this season'))
    badge_color = models.PositiveSmallIntegerField(default=0)  # kolor metki na liście rezultatów - numer na liście par kolorów - jeden dla tła, drugi dla tekstu.

    class Meta:
        ordering = ['slug']

    def __str__(self):
        return "%s %s" % (self.name, self.game.slug)

    def get_player_pts(self, player):
        return MatchResult.objects.filter(player=player, match__season=self).aggregate(points=Sum('points'))['points']

    # def save(self, *args, **kwargs):
    #     # TODO: Umożliwić tworzenie kilku niezależnych lig do danej gry w danym sezonie.
    #     ls = LeagueSeason.objects.filter(Q(start_date__range=(self.start_date, self.end_date)) | Q(
    #         end_date__range=(self.start_date, self.end_date)) | Q(start_date__lte=self.start_date,
    #                                                               end_date__gte=self.end_date)).filter(game=self.game)
    #     if ls.count() > 1 or (ls.count() == 1 and ls[0].id != self.id):
    #         raise ValidationError(_('Only one LeagueSeason per game per time period allowed.'))
    #     else:
    #         super(LeagueSeason, self).save(*args, **kwargs)

    def get_player_points(self, player):
        """
        Sprawdź wynik (liczba punktów) danego gracza w danym sezonie.
        """
        return player.matchresult_set.filter(match__season=self, match__ignore=False).aggregate(points=SumWithDefault('points', default=0))['points']

    def get_player_rank(self, player):
        """
        Sprawdź wynik (pozycja w rankingu) danego gracza w danym sezonie.
        """
        results = dict([(person[0], int(i)+1) for i, person in enumerate(self.get_ranks())])
        return results.get(player.username)

    def get_ranks(self):
        "Zwraca listę graczy posortowaną po ich punktach w lidze."

        # ranks = LeagueSeason.objects.filter(id=self.id).values(
        #     'name', 'match__matchresult__player__username').annotate(points=SumWithDefault(
        #         'match__matchresult__points', default=0)).distinct().order_by(
        #             '-points').exclude(points=0)

        ranks = sorted([{'username': player.username, 'points': self.get_player_points(player)} for player in
                        self.players.filter(matchresult__points__gt=0, matchresult__match__season=self).distinct()],
                       key=lambda p: p['points'], reverse=True)
        ranks.extend([{'username': player.username, 'points': 0} for player in self.get_pointless()])

        return ranks

    def get_pointless(self):
        return self.players.exclude(matchresult__match__season=self,
                                    matchresult__points__gt=0).distinct().order_by('last_name')

    def howmanyplayed(self, player, opp):
        if not isinstance(player, Player):
            player = player.get('username', player)
            player = Player.objects.get(username=player)
        if not isinstance(opp, Player):
            opp = opp.get('username', opp)
            opp = Player.objects.get(username=opp)
        return self.match_set.filter(matchresult__player=player, ignore=False).filter(matchresult__player=opp, ignore=False).count()

    def enroll_player(self, player):
        try:
            LeagueEnroll.objects.create(player=player, season=self)
            return True
        except:
            return False

    @property
    def enroll_allowed(self):
        return self.end_date > now().date()

    def report_match(self, winner, loser, won, lost, when, category=None, ignore=False, multiplier=1, tournament=None):
        """
        Tutaj wypadałoby także załatwić kwestię ignorowania nadwyżkowych wyników
        """
        if won < 0 or lost < 0:
            raise Exception('Próba zgłoszenia niepoprawnego meczu!')
        if category is None or not isinstance(category, EventCategory):
            category = self.default_match_category
        if not ignore:
            points_for_win = int(self.game.points_for_winning)
            points_for_loss = int(self.game.points_for_losing)
            points_for_draw = int(self.game.points_for_draw)
        else:
            points_for_win = points_for_loss = points_for_draw = 0

        win_reward = self.win_reward
        lose_reward = self.lose_reward
        draw_reward = self.draw_reward

        if callable(when):
            when = when()

        num = Match.objects.filter(season=self, players=winner).filter(players=loser).exclude(ignore=True).distinct().count()
        if num < self.max_matches:
            ignore = False
        else:
            ignore = True
        mecz = Match.objects.create(category=category, season=self, when=when, ignore=ignore, tournament=tournament)
        params = {}
        if won != lost:
            if win_reward and not ignore:
                r1 = Reward.objects.create(player=winner, category=win_reward, when=when,
                                           season=self, value=win_reward.value * multiplier,
                                           comment='[Automat] Wygrany mecz')
                params.update({'reward': r1})
            MatchResult.objects.create(player=winner, match=mecz, games_won=won,
                                       points=points_for_win, **params)
            params = {}
            if lose_reward and not ignore:
                r2 = Reward.objects.create(player=loser, category=lose_reward, when=when,
                                           season=self, value=lose_reward.value * multiplier,
                                           comment='[Automat] Rozegrany mecz')
                params.update({'reward': r2})
            MatchResult.objects.create(player=loser, match=mecz, games_won=lost,
                                       points=points_for_loss, **params)
        else:
            if draw_reward and not ignore:
                r1 = Reward.objects.create(player=winner, category=draw_reward, when=when,
                                           season=self,
                                           value=draw_reward.value * multiplier,
                                           comment='[Automat] Remis')
                params.update({'reward': r1})
            MatchResult.objects.create(player=winner, match=mecz, games_won=won,
                                       points=points_for_draw, **params)
            params = {}
            if draw_reward and not ignore:
                r2 = Reward.objects.create(player=loser, category=draw_reward, when=when,
                                           season=self, value=draw_reward.value * multiplier,
                                           comment='[Automat] Remis')
                params.update({'reward': r2})
            MatchResult.objects.create(player=loser, match=mecz, games_won=lost,
                                       points=points_for_draw, **params)

        # raise NotImplementedError('Method not implemented!')


@python_2_unicode_compatible
class LeagueEnroll(models.Model):
    uuid = ShortUUIDField()
    player = models.ForeignKey(settings.AUTH_USER_MODEL)
    season = models.ForeignKey(LeagueSeason)
    date = models.DateTimeField(default=now)
    reward_given = models.BooleanField(default=False, help_text=_('Czy gracz otrzymał już nagrodę za miejsce w lidze.'))

    class Meta:
        unique_together = ('player', 'season')

    def __str__(self):
        return "Player"


class EventCategory(models.Model):
    """
    Kategoria wydarzenia, np. casual, FNM, GPT itd.
    """
    uuid = ShortUUIDField()
    name = models.CharField(_('name'), max_length=64)
    enroll_reward = models.ForeignKey(RewardCategory, related_name='conf_as_enroll', verbose_name=_('enroll reward'),
                                      blank=True, null=True)
    reward_multiplier = models.PositiveSmallIntegerField(_('reward multiplier'), default=1)
    max_players = models.PositiveSmallIntegerField(_('max players'), default=2)  # liczba graczy na mecz

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class Reward(models.Model):
    """
    Pięczątki wbite do karnetu lub wykorzystane na zakup promek.
    """
    # player = ForeignKeyS2(Player, verbose_name=_('player'))
    player = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_('player'))
    uuid = ShortUUIDField()
    category = models.ForeignKey(RewardCategory, verbose_name=_('category'))
    season = models.ForeignKey(LeagueSeason)
    value = models.SmallIntegerField(_('value'), blank=True, null=True,
                                     help_text=_("Leave blank to copy category's value."))
    orig_value = models.SmallIntegerField(_('original value'), blank=True, null=True, editable=False)
    when = models.DateTimeField(_('when'), default=now)
    comment = models.TextField(_('comment'), blank=True, help_text=_("Leave blank to copy category's name."))

    def __str__(self):
        return "%s %s" % (self.player, self.comment)

    def save(self, *args, **kwargs):
        self.comment = self.comment or self.category.name
        if self.value is None:
            self.value = self.category.value
        if self.orig_value is None:
            self.orig_value = self.value

        # sprzedajemy promkę
        my_rewards = Reward.objects.filter(player=self.player, value__gt=0)
        v = my_rewards.aggregate(value=Sum('value'))['value']
        if v is not None and v > 0 and v > (0 - self.value):
            while self.value < 0:  # wydanie promki
                r = my_rewards.order_by('when')[0]
                if r.value >= abs(self.value):
                    r.value -= abs(self.value)
                    self.value = 0
                else:
                    self.value += r.value
                    r.value = 0
                r.save()
        super(Reward, self).save(*args, **kwargs)


@python_2_unicode_compatible
class PromoItem(models.Model):
    uuid = ShortUUIDField()
    name = models.CharField(_('name'), max_length=30)
    image = models.ImageField(_('image'), upload_to='promo_items')
    reward_category = models.ForeignKey(RewardCategory, verbose_name=_('reward category'))
    desc = models.TextField(_('description'), blank=True)

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class Tournament(models.Model):
    """
    Konfiguracja parsera wyników z danego narzędzia - ile czego za co ;)
    Przede wszystkim chodzi o to, żeby przyporządkować konkretne kategorie
    nagród za konkretne osiągnięcia - inaczej nie da się tego zrobić.
    """
    uuid = ShortUUIDField()
    name = models.CharField(_('name'), max_length=50, blank=True, null=True)
    # game = models.ForeignKey(Game, verbose_name=_('game'))
    season = models.ForeignKey(LeagueSeason, verbose_name=_('season'))
    category = models.ForeignKey('EventCategory')
    start_date = models.DateTimeField(_('start date'), blank=True, null=True)
    end_date = models.DateTimeField(_('end date'), blank=True, null=True)
    result = models.TextField(_('result'), blank=True, editable=False)
    reporter_tool = models.PositiveSmallIntegerField(_('reporter tool'), null=True, blank=True, editable=False)
    parsed = models.BooleanField(_('parsed'), editable=False, default=False,
                                 help_text=_('Oznaczenie, czy dany turniej został pomyślnie sparsowany'))

    def __str__(self):
        return self.name or '%s %s' % (self.season.game.slug, self.category.name)

    def save(self, *args, **kwargs):
        # przerobić to na signalsy jeśli gry będą w postaci pluginów
        change = bool(self.reporter_tool) and bool(self.result)
        if not change:
            self.reporter_tool = self.season.game.reporter_tool
            try:
                if self.reporter_tool and not self.parsed:
                    self.parse_results()
                    self.parsed = True
            except NotImplementedError:
                # przydałoby się zrobić jakiś rollback - usunąć mecze, które zostały dodane w tej (niepoprawnej) sesji
                pass
        super(Tournament, self).save(*args, **kwargs)

    def parse_results(self, debug=False, skip_enroll=False):
        if self.reporter_tool == 1:
            self._parse_reporter_results(debug, skip_enroll)
        else:
            raise NotImplementedError

    def _parse_reporter_results(self, debug=False, skip_enroll=False):
        # takie parsowanie rezultatów trzeba by przerzucić do osobnej klasy związanej z konkretną grą
        # gry powinno się wtedy dodawać na zasadzie pluginów
        tree = etree.fromstring(self.result).getroottree()
        rundy = tree.find('matches').findall('round')

        t_start = dateparser.parse(rundy[0].attrib['date'])  # data rozpoczęcia pierwszej rundy
        t_end = dateparser.parse(rundy[-1].attrib['date']) + datetime.timedelta(hours=1)  # przybliżony czas końca

        if self.start_date is None:
            self.start_date = t_start
        if self.end_date is None:
            self.end_date = t_end
        if debug:
            logger.info("Season: {}".format(self.season.name))
        enroll_reward = self.category.enroll_reward

        if not skip_enroll:
            for p in tree.iter('person'):
                try:
                    player = Player.objects.get(gameid__number=p.attrib['id'], gameid__game=self.season.game,
                                                leagueseason=self.season, leagueenroll__date__lte=t_start)
                    # nagradzamy przyjście na turniej
                    if debug:
                        logger.info("{} gets reward for enroll".format(player))
                    elif enroll_reward:
                        Reward.objects.create(player=player, category=enroll_reward, season=self.season,
                                              value=enroll_reward.value * self.category.reward_multiplier,
                                              comment='[Automat] Udział w turnieju')
                except Player.DoesNotExist:
                    # gracz nie jest zapisany w systemie
                    if debug:
                        logger.info("Player {} not found in system".format(p.attrib['id']))
                    continue
                    # do tego miejsca działa poprawnie
        for m in tree.iter('match'):
            opp = m.attrib.get('opponent', None)
            if debug:
                winner = loser = None
            if opp is None:
                # gracz miał bye
                if debug:
                    logger.info("{} got bye".format(m.attrib['person']))
                continue
            try:
                winner = Player.objects.get(gameid__number=m.attrib['person'], gameid__game=self.season.game,
                                            leagueseason=self.season, leagueenroll__date__lte=t_start)
                loser = Player.objects.get(gameid__number=m.attrib['opponent'], gameid__game=self.season.game,
                                           leagueseason=self.season, leagueenroll__date__lte=t_start)
                if debug:
                    logger.info("Winner: {winner}; Loser: {loser}".format(winner=six.text_type(winner),
                                                                          loser=six.text_type(loser)))
            except Player.DoesNotExist:
                # jeden z graczy nie jest zapisany w systemie lub nie gra w tym sezonie lub zapisał się później
                if debug:
                    logger.info("one of players not found in system")
                    logger.info("w: {winner}; l: {loser}".format(winner=six.text_type(winner), loser=six.text_type(loser)))
                # TODO: czy stworzyć wirtualne konto dla takiego gracza?
                continue

            runda = m.getparent()
            when = dateparser.parse(runda.attrib['date'])
            if debug:
                logger.info(
                    """Create match: category=%(category)s, season=%(season)s, when=%(when)s, tournament=%(tournament)s
                    raw_win: %(raw_win)s ; raw_loss: %(raw_loss)s""" % {
                        'category': self.category,
                        'season': self.season,
                        'when': when,
                        'tournament': self,
                        'raw_win': m.attrib['win'],
                        'raw_loss': m.attrib['loss']})
            else:
                # if num < self.season.max_matches:
                #     ignore = False
                # else:
                #     ignore = True
                won = m.attrib['win']
                lost = m.attrib['loss']
                self.season.report_match(winner=winner, loser=loser, won=won, lost=lost,
                                         category=self.category, when=when, tournament=self)

    def _parse_pokemon_results(self, debug=False, skip_enroll=False):
        raise NotImplementedError()


@python_2_unicode_compatible
class Match(models.Model):
    """
    Pojedynczy mecz do ligi.
    """
    uuid = ShortUUIDField()
    category = models.ForeignKey(EventCategory, verbose_name=_('category'), null=True, blank=True)
    season = models.ForeignKey(LeagueSeason, verbose_name=_('season'))
    tournament = models.ForeignKey(Tournament, verbose_name=_('tournament'), blank=True, null=True)
    players = models.ManyToManyField(settings.AUTH_USER_MODEL, through='MatchResult', verbose_name=_('players'))
    when = models.DateTimeField(_('when'), default=now)
    ignore = models.BooleanField(_('ignore'), default=False)
    # w takim układzie można zapisywać wszystkie mecze i ignorować te nadprogramowe - większa przejrzystość

    def __str__(self):
        return "Mecz z %s" % self.when


@python_2_unicode_compatible
class MatchResult(models.Model):
    """
    Rezultat meczu z punktu widzenia danego gracza.
    Dzięki takiemu rozłożeniu można teamy (np. 2HG) przerzucić na frontend.
    """
    uuid = ShortUUIDField()
    player = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_('player'))
    match = models.ForeignKey(Match, verbose_name=_('match'))
    games_won = models.PositiveSmallIntegerField(_('games won'))
    points = models.PositiveSmallIntegerField(_('points'), editable=False, default=0)
    reward = models.ForeignKey(Reward, null=True, blank=True, verbose_name=_('reward'))
    when = models.DateTimeField(_('when'), auto_now_add=True)

    def __str__(self):
        return "%s %s (%s) %sp" % (self.match, self.player, self.games_won, self.points)

